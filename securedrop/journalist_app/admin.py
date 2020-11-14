# -*- coding: utf-8 -*-

import os
from typing import Optional
from typing import Union

import werkzeug
from flask import (Blueprint, render_template, request, url_for, redirect, g,
                   current_app, flash, abort)
from flask_babel import gettext
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import NoResultFound

import i18n
from db import db
from html import escape
from models import (InstanceConfig, Journalist, InvalidUsernameException,
                    FirstOrLastNameError, PasswordError)
from journalist_app.decorators import admin_required
from journalist_app.utils import (commit_account_changes, set_diceware_password,
                                  validate_hotp_secret, revoke_token)
from journalist_app.forms import LogoForm, NewUserForm, SubmissionPreferencesForm, OrgNameForm
from sdconfig import SDConfig
from passphrases import PassphraseGenerator


def make_blueprint(config: SDConfig) -> Blueprint:
    view = Blueprint('admin', __name__)

    @view.route('/', methods=('GET', 'POST'))
    @admin_required
    def index() -> str:
        users = Journalist.query.all()
        return render_template("admin.html", users=users)

    @view.route('/config', methods=('GET', 'POST'))
    @admin_required
    def manage_config() -> Union[str, werkzeug.Response]:
        # The UI prompt ("prevent") is the opposite of the setting ("allow"):
        submission_preferences_form = SubmissionPreferencesForm(
            prevent_document_uploads=not current_app.instance_config.allow_document_uploads)
        organization_name_form = OrgNameForm(
            organization_name=current_app.instance_config.organization_name)
        logo_form = LogoForm()
        if logo_form.validate_on_submit():
            f = logo_form.logo.data

            if current_app.static_folder is None:
                abort(500)
            custom_logo_filepath = os.path.join(current_app.static_folder, 'i',
                                                'custom_logo.png')
            try:
                f.save(custom_logo_filepath)
                flash(gettext("Image updated."), "logo-success")
            except Exception:
                flash("Unable to process the image file."
                      " Try another one.", "logo-error")
            finally:
                return redirect(url_for("admin.manage_config"))
        else:
            for field, errors in list(logo_form.errors.items()):
                for error in errors:
                    flash(error, "logo-error")
            return render_template("config.html",
                                   submission_preferences_form=submission_preferences_form,
                                   organization_name_form=organization_name_form,
                                   logo_form=logo_form)

    @view.route('/update-submission-preferences', methods=['POST'])
    @admin_required
    def update_submission_preferences() -> Optional[werkzeug.Response]:
        form = SubmissionPreferencesForm()
        if form.validate_on_submit():
            # The UI prompt ("prevent") is the opposite of the setting ("allow"):
            flash(gettext("Preferences saved."), "submission-preferences-success")
            value = not bool(request.form.get('prevent_document_uploads'))
            InstanceConfig.set_allow_document_uploads(value)
            return redirect(url_for('admin.manage_config'))
        else:
            for field, errors in list(form.errors.items()):
                for error in errors:
                    flash(gettext("Preferences not updated.") + " " + error,
                          "submission-preferences-error")
        return redirect(url_for('admin.manage_config'))

    @view.route('/update-org-name', methods=['POST'])
    @admin_required
    def update_org_name() -> Union[str, werkzeug.Response]:
        form = OrgNameForm()
        if form.validate_on_submit():
            try:
                value = request.form['organization_name']
                InstanceConfig.set_organization_name(escape(value, quote=True))
                flash(gettext("Preferences saved."), "org-name-success")
            except Exception:
                flash(gettext('Failed to update organization name.'), 'org-name-error')
            return redirect(url_for('admin.manage_config'))
        else:
            for field, errors in list(form.errors.items()):
                for error in errors:
                    flash(error, "org-name-error")
        return redirect(url_for('admin.manage_config'))

    @view.route('/add', methods=('GET', 'POST'))
    @admin_required
    def add_user() -> Union[str, werkzeug.Response]:
        form = NewUserForm()
        if form.validate_on_submit():
            form_valid = True
            username = request.form['username']
            first_name = request.form['first_name']
            last_name = request.form['last_name']
            password = request.form['password']
            is_admin = bool(request.form.get('is_admin'))

            try:
                otp_secret = None
                if request.form.get('is_hotp', False):
                    otp_secret = request.form.get('otp_secret', '')
                new_user = Journalist(username=username,
                                      password=password,
                                      first_name=first_name,
                                      last_name=last_name,
                                      is_admin=is_admin,
                                      otp_secret=otp_secret)
                db.session.add(new_user)
                db.session.commit()
            except PasswordError:
                flash(gettext(
                      'There was an error with the autogenerated password. '
                      'User not created. Please try again.'), 'error')
                form_valid = False
            except InvalidUsernameException as e:
                form_valid = False
                flash('Invalid username: ' + str(e), "error")
            except IntegrityError as e:
                db.session.rollback()
                form_valid = False
                if "UNIQUE constraint failed: journalists.username" in str(e):
                    flash(gettext('Username "{user}" already taken.'.format(
                        user=username)), "error")
                else:
                    flash(gettext("An error occurred saving this user"
                                  " to the database."
                                  " Please inform your admin."),
                          "error")
                    current_app.logger.error("Adding user "
                                             "'{}' failed: {}".format(
                                                 username, e))

            if form_valid:
                return redirect(url_for('admin.new_user_two_factor',
                                        uid=new_user.id))

        password = PassphraseGenerator.get_default().generate_passphrase(
            preferred_language=i18n.get_language(config)
        )
        return render_template("admin_add_user.html",
                               password=password,
                               form=form)

    @view.route('/2fa', methods=('GET', 'POST'))
    @admin_required
    def new_user_two_factor() -> Union[str, werkzeug.Response]:
        user = Journalist.query.get(request.args['uid'])

        if request.method == 'POST':
            token = request.form['token']
            if user.verify_token(token):
                flash(gettext(
                    "The two-factor code for user \"{user}\" was verified "
                    "successfully.").format(user=user.username),
                    "notification")
                return redirect(url_for("admin.index"))
            else:
                flash(gettext(
                    "There was a problem verifying the two-factor code. Please try again."),
                      "error")

        return render_template("admin_new_user_two_factor.html", user=user)

    @view.route('/reset-2fa-totp', methods=['POST'])
    @admin_required
    def reset_two_factor_totp() -> werkzeug.Response:
        uid = request.form['uid']
        user = Journalist.query.get(uid)
        user.is_totp = True
        user.regenerate_totp_shared_secret()
        db.session.commit()
        return redirect(url_for('admin.new_user_two_factor', uid=uid))

    @view.route('/reset-2fa-hotp', methods=['POST'])
    @admin_required
    def reset_two_factor_hotp() -> Union[str, werkzeug.Response]:
        uid = request.form['uid']
        otp_secret = request.form.get('otp_secret', None)
        if otp_secret:
            user = Journalist.query.get(uid)
            if not validate_hotp_secret(user, otp_secret):
                return render_template('admin_edit_hotp_secret.html', uid=uid)
            db.session.commit()
            return redirect(url_for('admin.new_user_two_factor', uid=uid))
        else:
            return render_template('admin_edit_hotp_secret.html', uid=uid)

    @view.route('/edit/<int:user_id>', methods=('GET', 'POST'))
    @admin_required
    def edit_user(user_id: int) -> Union[str, werkzeug.Response]:
        user = Journalist.query.get(user_id)

        if request.method == 'POST':
            if request.form.get('username', None):
                new_username = request.form['username']

                try:
                    Journalist.check_username_acceptable(new_username)
                except InvalidUsernameException as e:
                    flash('Invalid username: ' + str(e), 'error')
                    return redirect(url_for("admin.edit_user",
                                            user_id=user_id))

                if new_username == user.username:
                    pass
                elif Journalist.query.filter_by(
                        username=new_username).one_or_none():
                    flash(gettext(
                        'Username "{user}" already taken.').format(
                            user=new_username),
                        "error")
                    return redirect(url_for("admin.edit_user",
                                            user_id=user_id))
                else:
                    user.username = new_username

            try:
                first_name = request.form['first_name']
                Journalist.check_name_acceptable(first_name)
                user.first_name = first_name
            except FirstOrLastNameError as e:
                flash(gettext('Name not updated: {}'.format(e)), "error")
                return redirect(url_for("admin.edit_user", user_id=user_id))

            try:
                last_name = request.form['last_name']
                Journalist.check_name_acceptable(last_name)
                user.last_name = last_name
            except FirstOrLastNameError as e:
                flash(gettext('Name not updated: {}'.format(e)), "error")
                return redirect(url_for("admin.edit_user", user_id=user_id))

            user.is_admin = bool(request.form.get('is_admin'))

            commit_account_changes(user)

        password = PassphraseGenerator.get_default().generate_passphrase(
            preferred_language=i18n.get_language(config)
        )
        return render_template("edit_account.html", user=user,
                               password=password)

    @view.route('/delete/<int:user_id>', methods=('POST',))
    @admin_required
    def delete_user(user_id: int) -> werkzeug.Response:
        user = Journalist.query.get(user_id)
        if user_id == g.user.id:
            # Do not flash because the interface already has safe guards.
            # It can only happen by manually crafting a POST request
            current_app.logger.error(
                "Admin {} tried to delete itself".format(g.user.username))
            abort(403)
        elif user:
            db.session.delete(user)
            db.session.commit()
            flash(gettext("Deleted user '{user}'.").format(
                user=user.username), "notification")
        else:
            current_app.logger.error(
                "Admin {} tried to delete nonexistent user with pk={}".format(
                    g.user.username, user_id))
            abort(404)

        return redirect(url_for('admin.index'))

    @view.route('/edit/<int:user_id>/new-password', methods=('POST',))
    @admin_required
    def new_password(user_id: int) -> werkzeug.Response:
        try:
            user = Journalist.query.get(user_id)
        except NoResultFound:
            abort(404)

        password = request.form.get('password')
        if set_diceware_password(user, password) is not False:
            if user.last_token is not None:
                revoke_token(user, user.last_token)
            user.session_nonce += 1
            db.session.commit()
        return redirect(url_for('admin.edit_user', user_id=user_id))

    @view.route('/ossec-test')
    @admin_required
    def ossec_test() -> werkzeug.Response:
        current_app.logger.error('This is a test OSSEC alert')
        flash(gettext('Test alert sent. Please check your email.'),
              'notification')
        return redirect(url_for('admin.manage_config'))

    return view
