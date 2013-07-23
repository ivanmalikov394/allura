#       Licensed to the Apache Software Foundation (ASF) under one
#       or more contributor license agreements.  See the NOTICE file
#       distributed with this work for additional information
#       regarding copyright ownership.  The ASF licenses this file
#       to you under the Apache License, Version 2.0 (the
#       "License"); you may not use this file except in compliance
#       with the License.  You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#       Unless required by applicable law or agreed to in writing,
#       software distributed under the License is distributed on an
#       "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
#       KIND, either express or implied.  See the License for the
#       specific language governing permissions and limitations
#       under the License.

import logging
from urllib import basejoin
from cStringIO import StringIO

from tg import expose, redirect, flash
from tg.decorators import without_trailing_slash
from pylons import request, app_globals as g, tmpl_context as c
from paste.deploy.converters import asbool, asint
from bson import ObjectId

from ming.orm import session, state
from ming.utils import LazyProperty

from allura.lib import helpers as h
from allura.lib.security import require, has_access, require_access
from allura import model
from allura.controllers import BaseController
from allura.lib.decorators import require_post, event_handler
from allura.lib.utils import permanent_redirect

log = logging.getLogger(__name__)


class ConfigOption(object):
    """Definition of a configuration option for an :class:`Application`.

    """
    def __init__(self, name, ming_type, default, label=None):
        """Create a new ConfigOption.

        """
        self.name, self.ming_type, self._default, self.label = (
            name, ming_type, default, label or name)

    @property
    def default(self):
        """Return the default value for this ConfigOption.

        """
        if callable(self._default):
            return self._default()
        return self._default


class SitemapEntry(object):
    """A labeled URL, which may optionally have
    :class:`children <SitemapEntry>`.

    Used for generating trees of links.

    """
    def __init__(self, label, url=None, children=None, className=None,
            ui_icon=None, small=None, tool_name=None, matching_urls=None):
        """Create a new SitemapEntry.

        """
        self.label = label
        self.className = className
        if url is not None:
            url = url.encode('utf-8')
        self.url = url
        self.small = small
        self.ui_icon = ui_icon
        self.children = children or []
        self.tool_name = tool_name
        self.matching_urls = matching_urls or []

    def __getitem__(self, x):
        """Automatically expand the list of sitemap child entries with the
        given items.  Example::

            SitemapEntry('HelloForge')[
                SitemapEntry('foo')[
                    SitemapEntry('Pages')[pages]
                ]
            ]

        TODO: deprecate this; use a more clear method of building a tree

        """
        if isinstance(x, (list, tuple)):
            self.children.extend(list(x))
        else:
            self.children.append(x)
        return self

    def __repr__(self):
        l = ['<SitemapEntry ']
        l.append('    label=%r' % self.label)
        l.append('    url=%r' % self.url)
        l.append('    children=%s' % repr(self.children).replace('\n', '\n    '))
        l.append('>')
        return '\n'.join(l)

    def bind_app(self, app):
        """Recreate this SitemapEntry in the context of
        :class:`app <Application>`.

        :returns: :class:`SitemapEntry`

        """
        lbl = self.label
        url = self.url
        if callable(lbl):
            lbl = lbl(app)
        if url is not None:
            url = basejoin(app.url, url)
        return SitemapEntry(lbl, url, [
                ch.bind_app(app) for ch in self.children],
                className=self.className,
                ui_icon=self.ui_icon,
                small=self.small,
                tool_name=self.tool_name,
                matching_urls=self.matching_urls)

    def extend(self, sitemap_entries):
        """Extend our children with ``sitemap_entries``.

        :param sitemap_entries: list of :class:`SitemapEntry`

        For each entry, if it doesn't already exist in our children, add it.
        If it does already exist in our children, recursively extend the
        children or our copy with the children of the new copy.

        """
        child_index = dict(
            (ch.label, ch) for ch in self.children)
        for e in sitemap_entries:
            lbl = e.label
            match = child_index.get(e.label)
            if match and match.url == e.url:
                match.extend(e.children)
            else:
                self.children.append(e)
                child_index[lbl] = e

    def matches_url(self, request):
        """Return True if this SitemapEntry 'matches' the url of ``request``.

        """
        return self.url in request.upath_info or any([
            url in request.upath_info for url in self.matching_urls])


class Application(object):
    """
    The base Allura pluggable application

    After extending this, expose the app by adding an entry point in your
    setup.py:

        [allura]
        myapp = foo.bar.baz:MyAppClass

    :cvar str status: One of 'production', 'beta', 'alpha', or 'user'. By
        default, only 'production' apps are installable in projects. Default
        is 'production'.
    :cvar bool searchable: If True, show search box in the left menu of this
        Application. Default is True.
    :cvar list permissions: Named permissions used by instances of this
        Application. Default is [].
    :cvar dict permissions_desc: Descriptions of the named permissions.
    :cvar bool installable: Default is True, Application can be installed in
        projects.
    :cvar bool hidden: Default is False, Application is not hidden from the
        list of a project's installed tools.
    :cvar str tool_description: Text description of this Application.
    :cvar bool relaxed_mount_points: Set to True to relax the default mount point
        naming restrictions for this Application. Default is False. See
        :attr:`default mount point naming rules <allura.lib.helpers.re_tool_mount_point>` and
        :attr:`relaxed mount point naming rules <allura.lib.helpers.re_relaxed_tool_mount_point>`.
    :cvar Controller root: Serves content at
        /<neighborhood>/<project>/<app>/. Default is None - subclasses should
        override.
    :cvar Controller api_root: Serves API access at
        /rest/<neighborhood>/<project>/<app>/. Default is None - subclasses
        should override to expose API access to the Application.
    :ivar Controller admin: Serves admin functions at
        /<neighborhood>/<project>/<admin>/<app>/. Default is a
        :class:`DefaultAdminController` instance.
    :cvar dict icons: Mapping of icon sizes to application-specific icon paths.
    """

    __version__ = None
    config_options = [
        ConfigOption('mount_point', str, 'app'),
        ConfigOption('mount_label', str, 'app'),
        ConfigOption('ordinal', int, '0')]
    status_map = ['production', 'beta', 'alpha', 'user']
    status = 'production'
    script_name = None
    root = None  # root controller
    api_root = None
    permissions = []
    permissions_desc = {
        'unmoderated_post': 'Post comments without moderation.',
        'post': 'Post comments, subject to moderation.',
        'moderate': 'Moderate comments.',
        'configure': 'Set label and options. Requires admin permission.',
        'admin': 'Set permissions.',
    }
    installable = True
    searchable = False
    DiscussionClass = model.Discussion
    PostClass = model.Post
    AttachmentClass = model.DiscussionAttachment
    tool_label = 'Tool'
    tool_description = "This is a tool for Allura forge."
    default_mount_label = 'Tool Name'
    default_mount_point = 'tool'
    relaxed_mount_points = False
    ordinal = 0
    hidden = False
    icons = {
        24:'images/admin_24.png',
        32:'images/admin_32.png',
        48:'images/admin_48.png'
    }

    def __init__(self, project, app_config_object):
        """Create an Application instance.

        :param project: Project to which this Application belongs
        :type project: :class:`allura.model.project.Project`
        :param app_config_object: Config describing this Application
        :type app_config_object: :class:`allura.model.project.AppConfig`

        """
        self.project = project
        self.config = app_config_object
        self.admin = DefaultAdminController(self)

    @LazyProperty
    def sitemap(self):
        """Return a list of :class:`SitemapEntries <allura.app.SitemapEntry>`
        describing the page hierarchy provided by this Application.

        If the list is empty, the Application will not be displayed in the
        main project nav bar.

        """
        return [SitemapEntry(self.config.options.mount_label, '.')]

    @LazyProperty
    def url(self):
        """Return the URL for this Application.

        """
        return self.config.url(project=self.project)

    @property
    def acl(self):
        """Return the :class:`Access Control List <allura.model.types.ACL>`
        for this Application.

        """
        return self.config.acl

    @classmethod
    def describe_permission(cls, permission):
        """Return help text describing what features ``permission`` controls.

        Subclasses should define :attr:`permissions_desc`,
        a ``{permission: description}`` mapping.

        Returns empty string if there is no description for ``permission``.

        """
        d = {}
        for t in reversed(cls.__mro__):
            d = dict(d, **getattr(t, 'permissions_desc', {}))
        return d.get(permission, '')

    def parent_security_context(self):
        """Return the parent of this object.

        Used for calculating permissions based on trees of ACLs.

        """
        return self.config.parent_security_context()

    @classmethod
    def validate_mount_point(cls, mount_point):
        """Check if ``mount_point`` is valid for this Application.

        In general, subclasses should not override this, but rather toggle
        the strictness of allowed mount point names by toggling
        :attr:`Application.relaxed_mount_points`.

        :param mount_point: the mount point to validate
        :type mount_point: str
        :rtype: A :class:`regex Match object <_sre.SRE_Match>` if the mount
                point is valid, else None

        """
        re = (h.re_relaxed_tool_mount_point if cls.relaxed_mount_points
                else h.re_tool_mount_point)
        return re.match(mount_point)

    @classmethod
    def status_int(self):
        """Return the :attr:`status` of this Application as an int.

        Used for sorting available Apps by status in the Admin interface.

        """
        return self.status_map.index(self.status)

    @classmethod
    def icon_url(self, size):
        """Return URL for icon of the given ``size``.

        Subclasses can define their own icons by overriding
        :attr:`icons` or by overriding this method (which, by default,
        returns the URLs defined in :attr:`icons`).

        """
        resource = self.icons.get(size)
        if resource:
            return g.theme_href(resource)
        return ''

    def has_access(self, user, topic):
        """Return True if ``user`` can send email to ``topic``.
        Default is False.

        :param user: :class:`allura.model.User` instance
        :param topic: str
        :rtype: bool

        """
        return False

    def is_visible_to(self, user):
        """Return True if ``user`` can view this app.

        :type user: :class:`allura.model.User` instance
        :rtype: bool

        """
        return has_access(self, 'read')(user=user)

    def subscribe_admins(self):
        """Subscribe all project Admins (for this Application's project) to the
        :class:`allura.model.notification.Mailbox` for this Application.

        """
        for uid in g.credentials.userids_with_named_role(self.project._id, 'Admin'):
            model.Mailbox.subscribe(
                type='direct',
                user_id=uid,
                project_id=self.project._id,
                app_config_id=self.config._id)

    def subscribe(self, user):
        """Subscribe :class:`user <allura.model.auth.User>` to the
        :class:`allura.model.notification.Mailbox` for this Application.

        """
        if user and user != model.User.anonymous():
            model.Mailbox.subscribe(
                    type='direct',
                    user_id=user._id,
                    project_id=self.project._id,
                    app_config_id=self.config._id)

    @classmethod
    def default_options(cls):
        """Return a ``(name, default value)`` mapping of this Application's
        :class:`config_options <ConfigOption>`.

        :rtype: dict

        """
        return dict(
            (co.name, co.default)
            for co in cls.config_options)

    def install(self, project):
        'Whatever logic is required to initially set up a tool'
        # Create the discussion object
        discussion = self.DiscussionClass(
            shortname=self.config.options.mount_point,
            name='%s Discussion' % self.config.options.mount_point,
            description='Forum for %s comments' % self.config.options.mount_point)
        session(discussion).flush()
        self.config.discussion_id = discussion._id
        self.subscribe_admins()

    def uninstall(self, project=None, project_id=None):
        'Whatever logic is required to tear down a tool'
        if project_id is None: project_id = project._id
        # De-index all the artifacts belonging to this tool in one fell swoop
        g.solr.delete(q='project_id_s:"%s" AND mount_point_s:"%s"' % (
                project_id, self.config.options['mount_point']))
        for d in model.Discussion.query.find({
                'project_id':project_id,
                'app_config_id':self.config._id}):
            d.delete()
        self.config.delete()
        session(self.config).flush()

    @property
    def uninstallable(self):
        """Return True if this app can be uninstalled. Controls whether the
        'Delete' option appears on the admin menu for this app.

        By default, an app can be uninstalled iff it can be installed, although
        some apps may want/need to override this (e.g. an app which can
        not be installed directly by a user, but may be uninstalled).

        """
        return self.installable

    def main_menu(self):
        """Return a list of :class:`SitemapEntries <allura.app.SitemapEntry>`
        to display in the main project nav for this Application.

        Default implementation returns :attr:`sitemap`.

        """
        return self.sitemap

    def sidebar_menu(self):
        """Return a list of :class:`SitemapEntries <allura.app.SitemapEntry>`
        to render in the left sidebar for this Application.

        """
        return []

    def sidebar_menu_js(self):
        """Return Javascript needed by the sidebar menu of this Application.

        :return: a string of Javascript code

        """
        return ""

    def admin_menu(self, force_options=False):
        """Return the admin menu for this Application.

        Default implementation will return a menu with up to 3 links:

            - 'Permissions', if the current user has admin access to the
                project in which this Application is installed
            - 'Options', if this Application has custom options, or
                ``force_options`` is True
            - 'Label', for editing this Application's label

        Subclasses should override this method to provide additional admin
        menu items.

        :param force_options: always include an 'Options' link in the menu,
            even if this Application has no custom options
        :return: a list of :class:`SitemapEntries <allura.app.SitemapEntry>`

        """
        admin_url = c.project.url()+'admin/'+self.config.options.mount_point+'/'
        links = []
        if self.permissions and has_access(c.project, 'admin')():
            links.append(SitemapEntry('Permissions', admin_url + 'permissions'))
        if force_options or len(self.config_options) > 3:
            links.append(SitemapEntry('Options', admin_url + 'options', className='admin_modal'))
        links.append(SitemapEntry('Label', admin_url + 'edit_label', className='admin_modal'))
        return links

    def handle_message(self, topic, message):
        """Handle incoming email msgs addressed to this tool.
        Default is a no-op.

        :param topic: portion of destination email address preceeding the '@'
        :type topic: str
        :param message: parsed email message
        :type message: dict - result of
            :func:`allura.lib.mail_util.parse_message`
        :rtype: None

        """
        pass

    def handle_artifact_message(self, artifact, message):
        """Handle message addressed to this Application.

        :param artifact: Specific artifact to which the message is addressed
        :type artifact: :class:`allura.model.artifact.Artifact`
        :param message: the message
        :type message: :class:`allura.model.artifact.Message`

        Default implementation posts the message to the appropriate discussion
        thread for the artifact.

        """
        # Find ancestor comment and thread
        thd, parent_id = artifact.get_discussion_thread(message)
        # Handle attachments
        message_id = message['message_id']
        if message.get('filename'):
            # Special case - the actual post may not have been created yet
            log.info('Saving attachment %s', message['filename'])
            fp = StringIO(message['payload'])
            self.AttachmentClass.save_attachment(
                message['filename'], fp,
                content_type=message.get('content_type', 'application/octet-stream'),
                discussion_id=thd.discussion_id,
                thread_id=thd._id,
                post_id=message_id,
                artifact_id=message_id)
            return
        # Handle duplicates
        post = self.PostClass.query.get(_id=message_id)
        if post:
            log.info('Existing message_id %s found - saving this as text attachment' % message_id)
            fp = StringIO(message['payload'])
            post.attach(
                'alternate', fp,
                content_type=message.get('content_type', 'application/octet-stream'),
                discussion_id=thd.discussion_id,
                thread_id=thd._id,
                post_id=message_id)
        else:
            text=message['payload'] or '--no text body--'
            post = thd.post(
                message_id=message_id,
                parent_id=parent_id,
                text=text,
                subject=message['headers'].get('Subject', 'no subject'))


class DefaultAdminController(BaseController):
    """Provides basic admin functionality for an :class:`Application`.

    To add more admin functionality for your Application, extend this
    class and then assign an instance of it to the ``admin`` attr of
    your Application::

        class MyApp(Application):
            def __init__(self, *args):
                super(MyApp, self).__init__(*args)
                self.admin = MyAdminController(self)

    """
    def __init__(self, app):
        """Instantiate this controller for an :class:`app <Application>`.

        """
        self.app = app

    @expose()
    def index(self, **kw):
        """Home page for this controller.

        Redirects to the 'permissions' page by default.

        """
        permanent_redirect('permissions')

    @expose('jinja:allura:templates/app_admin_permissions.html')
    @without_trailing_slash
    def permissions(self):
        """Render the permissions management web page.

        """
        from ext.admin.widgets import PermissionCard
        c.card = PermissionCard()
        permissions = dict((p, []) for p in self.app.permissions)
        for ace in self.app.config.acl:
            if ace.access == model.ACE.ALLOW:
                try:
                    permissions[ace.permission].append(ace.role_id)
                except KeyError:
                    # old, unknown permission
                    pass
        return dict(
            app=self.app,
            allow_config=has_access(c.project, 'admin')(),
            permissions=permissions)

    @expose('jinja:allura:templates/app_admin_edit_label.html')
    def edit_label(self):
        """Renders form to update the Application's ``mount_label``.

        """
        return dict(
            app=self.app,
            allow_config=has_access(self.app, 'configure')())

    @expose()
    @require_post()
    def update_label(self, mount_label):
        """Handles POST to update the Application's ``mount_label``.

        """
        require_access(self.app, 'configure')
        self.app.config.options['mount_label'] = mount_label
        redirect(request.referer)

    @expose('jinja:allura:templates/app_admin_options.html')
    def options(self):
        """Renders form to update the Application's ``config.options``.

        """
        return dict(
            app=self.app,
            allow_config=has_access(self.app, 'configure')())

    @expose()
    @require_post()
    def configure(self, **kw):
        """Handle POST to delete the Application or update its
        ``config.options``.

        """
        with h.push_config(c, app=self.app):
            require_access(self.app, 'configure')
            is_admin = self.app.config.tool_name == 'admin'
            if kw.pop('delete', False):
                if is_admin:
                    flash('Cannot delete the admin tool, sorry....')
                    redirect('.')
                c.project.uninstall_app(self.app.config.options.mount_point)
                redirect('..')
            for opt in self.app.config_options:
                if opt in Application.config_options:
                    continue  # skip base options (mount_point, mount_label, ordinal)
                val = kw.get(opt.name, '')
                if opt.ming_type == bool:
                    val = asbool(val or False)
                elif opt.ming_type == int:
                    val = asint(val or 0)
                self.app.config.options[opt.name] = val
            if is_admin:
                # possibly moving admin mount point
                redirect('/'
                         + c.project._id
                         + self.app.config.options.mount_point
                         + '/'
                         + self.app.config.options.mount_point
                         + '/')
            else:
                redirect(request.referer)

    @without_trailing_slash
    @expose()
    @h.vardec
    @require_post()
    def update(self, card=None, **kw):
        """Handle POST to update permissions for the Application.

        """
        old_acl = self.app.config.acl
        self.app.config.acl = []
        for args in card:
            perm = args['id']
            new_group_ids = args.get('new', [])
            del_group_ids = []
            group_ids = args.get('value', [])
            if isinstance(new_group_ids, basestring):
                new_group_ids = [ new_group_ids ]
            if isinstance(group_ids, basestring):
                group_ids = [ group_ids ]

            for acl in old_acl:
                if (acl['permission']==perm) and (str(acl['role_id']) not in group_ids):
                    del_group_ids.append(str(acl['role_id']))

            if new_group_ids or del_group_ids:
                model.AuditLog.log('updated "%s" permission: "%s" => "%s" for %s' % (
                    perm,
                    ', '.join(map(lambda id: model.ProjectRole.query.get(_id=ObjectId(id)).name, group_ids+del_group_ids)),
                    ', '.join(map(lambda id: model.ProjectRole.query.get(_id=ObjectId(id)).name, group_ids+new_group_ids)),
                    self.app.config.options['mount_point']))

            role_ids = map(ObjectId, group_ids + new_group_ids)
            self.app.config.acl += [
                model.ACE.allow(r, perm) for r in role_ids]
        redirect(request.referer)
