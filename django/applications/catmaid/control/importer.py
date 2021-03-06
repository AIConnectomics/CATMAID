import glob
import os.path
import yaml
import urllib
import requests

from collections import OrderedDict, defaultdict

from django import forms
from django.db import connection
from django.db.models import Count
from django.conf import settings
from django.contrib.auth.models import User, Group
from django.contrib.admin.widgets import FilteredSelectMultiple
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.shortcuts import render_to_response
from django.utils.translation import ugettext as _

from formtools.wizard.views import SessionWizardView

from guardian.models import Permission
from guardian.shortcuts import get_perms_for_model, assign

from catmaid.models import (Class, Relation, ClassInstance, Project, Stack,
        ProjectStack, Overlay, StackClassInstance, TILE_SOURCE_TYPES)
from catmaid.fields import Double3D
from catmaid.control.common import urljoin
from catmaid.control.classification import get_classification_links_qs, \
        link_existing_classification, ClassInstanceClassInstanceProxy

TEMPLATES = {"pathsettings": "catmaid/import/setup_path.html",
             "projectselection": "catmaid/import/setup_projects.html",
             "classification": "catmaid/import/setup_classification.html",
             "confirmation": "catmaid/import/confirmation.html"}

info_file_name = "project.yaml"
datafolder_setting = "CATMAID_IMPORT_PATH"
base_url_setting = "IMPORTER_DEFAULT_IMAGE_BASE"

class UserProxy(User):
    """ A proxy class for the user model as we want to be able to call
    get_name() on a user object and let it return the user name.
    """
    class Meta:
        proxy = True

    def get_name(self):
        return self.username

class GroupProxy(Group):
    """ A proxy class for the group model as we want to be able to call
    get_name() on a group object and let it return the group name.
    """
    class Meta:
        proxy = True

    def get_name(self):
        return self.name

class ImageBaseMixin:
    def set_image_fields(self, info_object, project_url=None, data_folder=None, needs_zoom=True):
        """ Sets the image_base, num_zoom_levels, file_extension fields
        of the calling object. Favor a URL field, if there is one. A URL
        field, however, also requires the existence of the
        'fileextension' and 'zoomlevels' field.
        """
        zoom_available = 'zoomlevels' in info_object
        ext_available = 'fileextension' in info_object

        if 'url' in info_object:
            # Make sure all required data is available
            required_fields = ['fileextension',]
            if needs_zoom:
                required_fields.append('zoomlevels')
            for f in required_fields:
                if f not in info_object:
                    raise RuntimeError("Missing required stack/overlay " \
                            "field '%s'" % f)
            # Read out data
            self.image_base = info_object['url']
            if needs_zoom:
                self.num_zoom_levels = info_object['zoomlevels']
            self.file_extension = info_object['fileextension']
        elif project_url:
            # The image base of a stack is the combination of the
            # project URL and the stack's folder name.
            folder = info_object['folder']
            self.image_base = urljoin(project_url, folder)
            # Favor 'zoomlevel' and 'fileextension' fields, if
            # available, but try to find this information if those
            # fields are not present.
            if zoom_available and needs_zoom:
                self.num_zoom_levels = info_object['zoomlevels']
            if ext_available:
                self.file_extension = info_object['fileextension']

        if data_folder:
            # Only retrieve file extension and zoom level if one of
            # them is not available in the stack definition.
            if (not zoom_available and needs_zoom) or not ext_available:
                file_ext, zoom_levels = find_zoom_levels_and_file_ext(
                    data_folder, folder, needs_zoom )
                # If there is no zoom level provided, use the found one
                if not zoom_available and needs_zoom:
                    if not zoom_levels:
                        raise RuntimeError("Missing required stack/overlay " \
                                "field 'zoomlevels' and couldn't retrieve " \
                                "this information from image data.")
                    self.num_zoom_levels = zoom_levels
                # If there is no file extension level provided, use the
                # found one
                if not ext_available:
                    if not file_ext:
                        raise RuntimeError("Missing required stack/overlay " \
                                "field 'fileextension' and couldn't retrieve " \
                                "this information from image data.")
                    self.file_extension = file_ext

        # Make sure the image base has a trailing slash, because this is expected
        if self.image_base[-1] != '/':
            self.image_base = self.image_base + '/'

        # Test if the data is accessible through HTTP
        self.accessible = check_http_accessibility(self.image_base,
                self.file_extension, auth=None)

class PreOverlay(ImageBaseMixin):
    def __init__(self, info_object, project_url, data_folder):
        self.name = info_object['name']
        # Set default opacity, if available, defaulting to 0
        if 'defaultopacity' in info_object:
            self.default_opacity = info_object['defaultopacity']
        else:
            self.default_opacity = 0
        # Set 'image_base', 'num_zoom_levels' and 'fileextension'
        self.set_image_fields(info_object, project_url, data_folder, False)

class PreStackGroup():
    def __init__(self, info_object):
        self.name= info_object['name']
        self.relation = info_object['relation']
        valid_relations = ("has_view", "has_channel")
        if self.relation not in valid_relations:
            raise ValueError("Unsupported stack group relation: {}. Plese use "
                    "one of: {}.".format(self.relation, ", ".join(valid_relations)))

class PreStack(ImageBaseMixin):
    def __init__(self, info_object, project_url, data_folder, already_known=False):
        # Make sure everything is there
        required_fields = ['name', 'dimension', 'resolution']
        for f in required_fields:
            if f not in info_object:
                raise RuntimeError("Missing required stack field '%s'" % f)
        # Read out data
        self.name = info_object['name']
        # Set 'image_base', 'num_zoom_levels' and 'fileextension'
        self.set_image_fields(info_object, project_url, data_folder, True)
        # The 'dimension', 'resolution' and 'metadata' fields should
        # have every stack.
        self.dimension = info_object['dimension']
        self.resolution = info_object['resolution']
        self.metadata = info_object['metadata'] if 'metadata' in info_object else ""
        # Use tile size information if available
        if 'tile_width' in info_object:
            self.tile_width = info_object['tile_width']
        if 'tile_height' in info_object:
            self.tile_height = info_object['tile_height']
        if 'tile_source_type' in info_object:
            self.tile_source_type = info_object['tile_source_type']
        # Stacks can optionally contain a "translation" field, which can be used
        # to add an offset when the stack is linked to a project
        self.project_translation = info_object.get('translation', "(0,0,0)")
        # Make sure this dimension can be matched
        if not Double3D.tuple_pattern.match(self.project_translation):
            raise ValueError("Couldn't read translation value")
        # Add overlays to the stack, if those are declared
        self.overlays = []
        if 'overlays' in info_object:
            for overlay in info_object['overlays']:
                self.overlays.append(PreOverlay(overlay, project_url, data_folder))
        # Collect stack group information
        self.stackgroups = []
        if 'stackgroups' in info_object:
            for stackgroup in info_object['stackgroups']:
                self.stackgroups.append(PreStackGroup(stackgroup))

        # Don't be considered known by default
        self.already_known = already_known

    def equals(self, stack):
        return self.image_base == stack.image_base

    def get_matching_objects(self, stack):
        return Stack.objects.filter(image_base=self.image_base)

class PreProject:
    def __init__(self, properties, project_url, data_folder, already_known_pid=None):
        info = properties

        # Make sure everything is there
        if 'project' not in info:
            raise RuntimeError("Missing required container field 'project'")
        # Read out data
        p = info['project']

        # Make sure everything is there
        if 'name' not in p:
            raise RuntimeError("Missing required project field '%s'" % f)
        # Read out data
        self.name = p['name']
        self.stacks = []
        self.has_been_imported = False
        self.import_status = None
        for s in p['stacks']:
            self.stacks.append(PreStack(s, project_url, data_folder))

        # Don't be considered known by default
        self.already_known = already_known_pid != None
        self.already_known_pid = already_known_pid
        self.action = None

    def set_known_pid(self, pid):
        self.already_known = pid != None
        self.already_known_pid = pid

    def imports_stack(self, stack):
        for s in self.stacks:
            if s.equals(stack):
                return True
        return False


def find_zoom_levels_and_file_ext( base_folder, stack_folder, needs_zoom=True ):
    """ Looks at the first file of the first zoom level and
    finds out what the file extension as well as the maximum
    zoom level is.
    """
    # Make sure the base path, doesn't start with a separator
    if base_folder[0] == os.sep:
        base_folder = base_folder[1:]
    # Build paths
    datafolder_path = getattr(settings, datafolder_setting)
    project_path = os.path.join(datafolder_path, base_folder)
    stack_path = os.path.join(project_path, stack_folder)
    slice_zero_path = os.path.join(stack_path, "0")
    filter_path = os.path.join(slice_zero_path, "0_0_0.*")
    # Look for 0/0_0_0.* file
    found_file = None
    for current_file in glob.glob( filter_path ):
        if os.path.isdir( current_file ):
            continue
        found_file = current_file
        break
    # Give up if we didn't find something
    if found_file is None:
        return (None, None)
    # Find extension
    file_ext = os.path.splitext(found_file)[1][1:]
    # Look for zoom levels
    zoom_level = 1
    if needs_zoom:
        while True:
            file_name = "0_0_" + str(zoom_level) + "." + file_ext
            path = os.path.join(slice_zero_path, file_name)
            if os.path.exists(path):
                zoom_level = zoom_level + 1
            else:
                zoom_level = zoom_level - 1
                break
    return (file_ext, zoom_level)

def check_http_accessibility(image_base, file_extension, auth=None):
    """ Returns true if data below this image base can be accessed through HTTP.
    """
    slice_zero_url = urljoin(image_base, "0")
    first_file_url = urljoin(slice_zero_url, "0_0_0." + file_extension)
    try:
        response = requests.get(first_file_url, auth=auth)
    except IOError:
        return False
    return response.status_code == 200

def find_project_folders(image_base, path, filter_term, depth=1):
    """ Finds projects in a folder structure by testing for the presence of an
    info/project YAML file.
    """
    index = []
    projects = {}
    not_readable = []
    for current_file in glob.glob( os.path.join(path, filter_term) ):
        if os.path.isdir(current_file):
            # Check if the folder has a info file
            info_file = os.path.join(current_file, info_file_name)
            if os.path.exists(info_file):
                short_name = current_file.replace(settings.CATMAID_IMPORT_PATH, "")
                # Check if this folder is already known by testing if
                # there is a stack with the same image base as a subfolder
                # would have.
                try:
                    # Make sure we have slashes in our directery URL part
                    url_dir = short_name
                    if os.sep != '/':
                        url_dir = url_dir.replace("\\", "/")
                    project_url = urljoin(image_base, url_dir)
                    # Expect a YAML file
                    yaml_data = yaml.load_all(open(info_file))
                    for project_yaml_data in yaml_data:
                        project = PreProject(project_yaml_data, project_url,
                                short_name)
                        key = current_file
                        new_key_index = 1
                        while key in projects:
                            new_key_index += 1
                            key = "{} #{}".format(current_file, new_key_index)
                        # Remember this project if it isn't available yet
                        projects[key] = project
                        index.append((key, short_name))
                except Exception as e:
                    not_readable.append( (info_file, e) )
            elif depth > 1:
                # Recurse in subdir if requested
                index = index + find_files( current_file, depth - 1)
    return (index, projects, not_readable)

def get_projects_from_url(url, filter_term, headers=None, auth=None):
    if not url:
        raise ValueError("No URL provided")
    if auth and len(auth) != 2:
        raise ValueError("HTTP Authentication needs to be a 2-tuple")
    # Sanitize and add protocol, if not there
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "http://" + url

    index = []
    projects = {}
    not_readable = []

    # Ask remote server for data
    r = requests.get(url, headers=headers, auth=auth)
    content_type = r.headers['content-type']
    # Both YAML and JSON should end up in the same directories of directories
    # structure.
    if 'json' in content_type:
        content = r.json()
        for p in content:
            project = PreProject(p, None, None)
            short_name = project.name
            key = "{}-{}".format(url, short_name)
            projects[key] = project
            index.append((key, short_name))
    elif 'yaml' in content_type:
        content = yaml.load_all(r.content)
        for p in content:
            for pp in p:
                project = PreProject(pp, None, None)
                short_name = project.name
                key = "{}-{}".format(url, short_name)
                projects[key] = project
                index.append((key, short_name))
    else:
        raise ValueError("Unrecognized content type in response of remote: " +
            content_type, r.content)

    return (index, projects, not_readable)

class ProjectSelector(object):
    """Mark projects as either ignored, merged or replaced"""

    def __init__(self, projects, project_index, known_project_filter,
            known_project_strategy, cursor=None):
        self.ignored_projects = []
        self.merged_projects = []
        self.replacing_projects = []
        self.new_projects = []

        self.known_project_filter = known_project_filter
        self.known_project_strategy = known_project_strategy

        # Mark projects as known
        if projects and known_project_filter:
            # Mark known stacks, i.e. the ones having the same image base
            for key, p in projects.iteritems():
                for s in p.stacks:
                    s.already_known = Stack.objects.filter(image_base=s.image_base).exists()

            cursor = cursor or connection.cursor()
            # Mark all projects as known that have the same name as an already
            # existing project
            if 'name' in known_project_filter:
                ip_template = ",".join(("(%s,%s)",) * len(projects))
                ip_data = []
                for pi in project_index:
                    ip_data.append(pi[0])
                    ip_data.append(projects[pi[0]].name)
                cursor.execute("""
                    SELECT p.id, ip.key, ip.name
                    FROM project p
                    JOIN (VALUES {}) ip(key, name)
                        ON p.title = ip.name
                """.format(ip_template), ip_data)
                for row in cursor.fetchall():
                    known_project = row[1]
                    p = projects[known_project]
                    p.set_known_pid(row[0])

            # Mark all projects as known that have the same stacks assigned as
            # an already existing project.
            if 'stacks' in known_project_filter:
                # Get a mapping of stack ID sets to project IDs
                cursor.execute("""
                  SELECT project_id, array_agg(s.image_base)
                  FROM project_stack ps
                  JOIN stack s ON ps.stack_id = s.id
                  GROUP BY project_id
                  ORDER BY project_id
                """)
                stack_map = defaultdict(list)
                for row in cursor.fetchall():
                  stack_map[tuple(sorted(row[1]))].append(row[0])

                for key, p in projects.iteritems():
                    if p.already_known:
                        # Name matches have precedence
                        continue
                    # Mark project known if all of its stacks are known
                    all_stacks_known = all(s.already_known for s in p.stacks)
                    if all_stacks_known:
                        known_stack_image_bases = tuple(sorted(s.image_base for s in p.stacks))
                        known_projects = stack_map[known_stack_image_bases]
                        if known_projects:
                          # First one wins
                          p.set_known_pid(known_projects[0])

        # Check for each project if it is already known
        for key,name in project_index:
            p = projects[key]
            self.add_project(key, p)

    def add_project(self, key, project):
        project.action = self.known_project_strategy
        if not project.already_known:
            self.new_projects.append((key, project))
        elif 'add-anyway' == self.known_project_strategy:
            self.new_projects.append((key, project))
        elif 'ignore' == self.known_project_strategy:
            self.ignored_projects.append((key, project))
        elif 'merge' == self.known_project_strategy:
            self.merged_projects.append((key, project))
        elif 'merge-override' == self.known_project_strategy:
            self.merged_projects.append((key, project))
        elif 'replace' == self.known_project_strategy:
            self.replacing_projects.append((key, project))

class ImportingWizard(SessionWizardView):
    def get_template_names(self):
        return [TEMPLATES[self.steps.current]]

    def get_form(self, step=None, data=None, files=None):
        form = super(ImportingWizard, self).get_form(step, data, files)
        current_step = step or self.steps.current
        if current_step == 'pathsettings':
            pass
            # Pre-populate base URL field with settings variable, if available
            if hasattr(settings, base_url_setting):
                form.fields['base_url'].initial = getattr(settings, base_url_setting)
        elif current_step == 'projectselection':
            # Make sure there is data available. This is needed to test whether
            # the classification step should be shown with the help of the
            # show_classification_suggestions() function.
            cleaned_path_data = self.get_cleaned_data_for_step('pathsettings')
            if not cleaned_path_data:
                return form
            # Get the cleaned data from first step
            source = cleaned_path_data['import_from']
            path = cleaned_path_data['relative_path']
            remote_host = cleaned_path_data['remote_host']
            catmaid_host = cleaned_path_data['catmaid_host']
            api_key = cleaned_path_data['api_key'].strip()
            filter_term = cleaned_path_data['filter_term']
            http_auth_user = cleaned_path_data['http_auth_user'].strip()
            http_auth_pass = cleaned_path_data['http_auth_pass']
            known_project_filter = cleaned_path_data['known_project_filter']
            known_project_strategy = cleaned_path_data['known_project_strategy']
            base_url = cleaned_path_data['base_url']

            if len(filter_term.strip()) == 0:
                filter_term = "*"

            auth = None
            if http_auth_user and http_auth_pass:
                auth = (http_auth_user, http_auth_pass)

            if source == 'remote':
                project_index, projects, not_readable = get_projects_from_url(
                        remote_host, filter_term, auth=auth)
            if source == 'remote-catmaid':
                complete_catmaid_host = "{}{}{}".format(catmaid_host,
                    "" if catmaid_host[-1] == "/" else "/", "projects/export")
                headers = None
                if len(api_key) > 0:
                    headers = {
                        'X-Authorization': 'Token {}'.format(api_key)
                    }
                project_index, projects, not_readable = get_projects_from_url(
                        complete_catmaid_host, filter_term, headers, auth)
            else:
                # Get all folders that match the selected criteria
                data_dir = os.path.join(settings.CATMAID_IMPORT_PATH, path)
                if data_dir[-1] != os.sep:
                    data_dir = data_dir + os.sep
                project_index, projects, not_readable = find_project_folders(
                    base_url, data_dir, filter_term)

            # Handle known projects
            project_selector = ProjectSelector(projects, project_index, known_project_filter,
                    known_project_strategy)

            # Sort the index (wrt. short name) to be better readable
            # Save these settings in the form
            form.not_readable = not_readable
            form.new_projects       = np = project_selector.new_projects
            form.ignored_projects   = ip = project_selector.ignored_projects
            form.merged_projects    = mp = project_selector.merged_projects
            form.replacing_projects = rp = project_selector.replacing_projects
            self.projects = projects
            # Update the folder list and select all by default
            displayed_projects = [(t[0], t[1].name) for t in np + mp + rp];
            displayed_projects = sorted(displayed_projects, key=lambda key: key[1])
            form.displayed_projects = displayed_projects
            form.fields['projects'].choices = displayed_projects
            form.fields['projects'].initial = [i[0] for i in displayed_projects]
            # Get the available user permissions and update the list



            user_permissions = get_element_permissions(UserProxy, Project)
            form.user_permissions = user_permissions
            user_perm_tuples = get_element_permission_tuples(user_permissions)
            form.fields['user_permissions'].choices = user_perm_tuples
            # Get the available group permissions and update the list
            group_permissions = get_element_permissions(GroupProxy, Project)
            form.group_permissions = group_permissions
            group_perm_tuples = get_element_permission_tuples(group_permissions)
            form.fields['group_permissions'].choices = group_perm_tuples
        elif current_step == 'classification':
            # Get tag set and all projects within it
            tags = self.get_cleaned_data_for_step('projectselection')['tags']
            tags = frozenset([t.strip() for t in tags.split(',')])
            # Get all projects that have all those tags
            projects = Project.objects.filter( tags__name__in=tags ).annotate(
                repeat_count=Count("id") ).filter( repeat_count=len(tags) )
            # Get all classification graphs linked to those projects and add
            # them to a form for being selected.
            workspace = settings.ONTOLOGY_DUMMY_PROJECT_ID
            croots = {}
            for p in projects:
                links_qs = get_classification_links_qs(workspace, p.id)
                linked_croots = set([cici.class_instance_b for cici in links_qs])
                # Build up dictionary with all classifications mapping to their
                # linked projects.
                for cr in linked_croots:
                    try:
                        croots[cr].append(p)
                    except KeyError:
                        croots[cr] = []
                        croots[cr].append(p)
            # Remember graphs and projects
            form.cls_tags = tags
            form.cls_graph_map = croots
            self.id_to_cls_graph = {}
            # Create data structure for form field and id mapping
            cgraphs = []
            for cr in croots:
                # Create form field tuples
                name = "%s (%s)" % (cr.name, cr.id)
                cgraphs.append( (cr.id, name) )
                # Create ID to classification graph mapping
                self.id_to_cls_graph[cr.id] = cr
            form.fields['classification_graph_suggestions'].choices = cgraphs
            #form.fields['classification_graph_suggestions'].initial = [cg[0] for cg in cgraphs]

        return form

    def get_context_data(self, form, **kwargs):
        context = super(ImportingWizard, self).get_context_data(form=form, **kwargs)

        if self.steps:
            if self.steps.current == 'pathsettings':
                datafolder_missing = not hasattr(settings, datafolder_setting)
                base_url_missing = not hasattr(settings, base_url_setting)
                context.update({
                    'datafolder_setting': datafolder_setting,
                    'datafolder_missing': datafolder_missing,
                    'base_url_setting': base_url_setting,
                    'base_url_missing': base_url_missing,
                })
            elif self.steps.current == 'projectselection':
                context.update({
                    'displayed_projects': getattr(form, "displayed_projects", []),
                    'new_projects': getattr(form, "new_projects", []),
                    'not_readable': getattr(form, "not_readable", []),
                    'ignored_projects': getattr(form, "ignored_projects", []),
                    'merged_projects': getattr(form, "merged_projects", []),
                    'replacing_projects': getattr(form, "replacing_projects", [])
                })
            elif self.steps.current == 'classification':
                context.update({
                    'cls_graphs': form.cls_graph_map,
                    'cls_tags': form.cls_tags,
                })
            elif self.steps.current == 'confirmation':
                # Selected projects
                selected_paths = self.get_cleaned_data_for_step('projectselection')['projects']
                selected_projects = [ self.projects[p] for p in selected_paths ]
                # Tags
                tags = self.get_cleaned_data_for_step('projectselection')['tags']
                if len(tags.strip()) == 0:
                    tags = []
                else:
                    tags = [t.strip() for t in tags.split(',')]
                # Permissions
                user_permissions = self.get_cleaned_data_for_step(
                    'projectselection')['user_permissions']
                user_permissions = get_permissions_from_selection( User, user_permissions )
                group_permissions = self.get_cleaned_data_for_step(
                    'projectselection')['group_permissions']
                group_permissions = get_permissions_from_selection( Group, group_permissions )
                # Classification graph links
                link_cls_graphs = self.get_cleaned_data_for_step(
                    'projectselection')['link_classifications']
                if link_cls_graphs:
                    # Suggested graphs
                    sugg_cls_graph_ids = self.get_cleaned_data_for_step(
                        'classification')['classification_graph_suggestions']
                    cls_graphs_to_link = set( self.id_to_cls_graph[int(cgid)] \
                        for cgid in sugg_cls_graph_ids )
                    # Manually selected graphs
                    sel_cicis = self.get_cleaned_data_for_step(
                        'classification')['additional_links']
                    sel_cls_graphs = set(cici.class_instance_b for cici in sel_cicis)
                    cls_graphs_to_link.update(sel_cls_graphs)
                    # Store combined result in context
                    context.update({
                        'cls_graphs_to_link': cls_graphs_to_link,
                    })
                # Other settings
                max_num_stacks = 0
                default_tile_width = self.get_cleaned_data_for_step('projectselection')['default_tile_width']
                default_tile_height = self.get_cleaned_data_for_step('projectselection')['default_tile_height']
                for p in selected_projects:
                    if len(p.stacks) > max_num_stacks:
                        max_num_stacks = len(p.stacks)
                context.update({
                    'link_cls_graphs': link_cls_graphs,
                    'projects': selected_projects,
                    'max_num_stacks': max_num_stacks,
                    'tags': tags,
                    'user_permissions': user_permissions,
                    'group_permissions': group_permissions,
                    'tile_width': default_tile_width,
                    'tile_height': default_tile_height,
                })

        context.update({
            'title': "Importer",
            'settings': settings
        })

        return context

    def done(self, form_list, **kwargs):
        """ Will add the selected projects.
        """
        # Find selected projects
        selected_paths = self.get_cleaned_data_for_step('projectselection')['projects']
        selected_projects = [ self.projects[p] for p in selected_paths ]
        # Get permissions
        user_permissions = self.get_cleaned_data_for_step(
            'projectselection')['user_permissions']
        user_permissions = get_permissions_from_selection( User, user_permissions )
        group_permissions = self.get_cleaned_data_for_step(
            'projectselection')['group_permissions']
        group_permissions = get_permissions_from_selection( Group, group_permissions )
        permissions = user_permissions + group_permissions
        # Tags
        tags = self.get_cleaned_data_for_step('projectselection')['tags']
        tags = [t.strip() for t in tags.split(',')]
        # Classifications
        link_cls_graphs = self.get_cleaned_data_for_step(
            'projectselection')['link_classifications']
        cls_graph_ids = []
        if link_cls_graphs:
            cls_graph_ids = self.get_cleaned_data_for_step(
                'classification')['classification_graph_suggestions']
        # Get remaining properties
        project_selection_data = self.get_cleaned_data_for_step('projectselection')
        remove_unref_stack_data = project_selection_data['remove_unref_stack_data']
        default_tile_width = project_selection_data['default_tile_width']
        default_tile_height = project_selection_data['default_tile_height']
        default_tile_source_type = project_selection_data['default_tile_source_type']
        imported_projects, not_imported_projects = import_projects(
            self.request.user, selected_projects, tags,
            permissions, default_tile_width, default_tile_height,
            default_tile_source_type, cls_graph_ids, remove_unref_stack_data)
        # Show final page
        return render_to_response('catmaid/import/done.html', {
            'projects': selected_projects,
            'imported_projects': imported_projects,
            'not_imported_projects': not_imported_projects,
        })

def importer_admin_view(request, *args, **kwargs):
    """ Wraps the class based ImportingWizard view in a
    function based view.
    """
    forms = [("pathsettings", DataFileForm),
             ("projectselection", ProjectSelectionForm),
             ("classification", create_classification_linking_form()),
             ("confirmation", ConfirmationForm)]
    # Create view with condition for classification step. It should only
    # be displayed if selected in the step before.
    view = ImportingWizard.as_view(forms,
        condition_dict={'classification': show_classification_suggestions})
    return view(request)

def show_classification_suggestions(wizard):
    """ This method tests whether the classification linking step should
    be shown or not.
    """
    cleaned_data = wizard.get_cleaned_data_for_step('projectselection') \
        or {'link_classifications': False}
    return cleaned_data['link_classifications']

def importer_finish(request):
    return render_to_response('catmaid/import/done.html', {})

def get_elements_with_perms_cls(element, cls, attach_perms=False):
    """ This is a slightly adapted version of guardians
    group retrieval. It doesn't need an object instance.
    """
    ctype = ContentType.objects.get_for_model(cls)
    if not attach_perms:
        return element.objects.all()
    else:
        elements = {}
        for elem in get_elements_with_perms_cls(element, cls):
            if not elem in elements:
                elements[elem] = get_perms_for_model(cls)
        return elements

def get_element_permissions(element, cls):
    elem_perms = get_elements_with_perms_cls(element, cls, True)
    sorted_elem_perms = OrderedDict(sorted(elem_perms.items(),
                                           key=lambda x: x[0].get_name()))
    return sorted_elem_perms

def get_element_permission_tuples(element_perms):
    """ Out of list of (element, [permissions] tuples, produce a
    list of tuples, each of the form
    (<element_id>_<perm_codename>, <element_name> | <parm_name>)
    """
    tuples = []
    for e in element_perms:
        for p in element_perms[e]:
            pg_id = str(e.id) + "_" + str(p.id)
            pg_title = e.get_name() + " | " + p.name
            tuples.append( (pg_id, pg_title) )
    return tuples

def get_permissions_from_selection(cls, selection):
    permission_list = []
    for perm in selection:
        elem_id = perm[:perm.index('_')]
        elem = cls.objects.filter(id=elem_id)[0]
        perm_id = perm[perm.index('_')+1:]
        perm = Permission.objects.filter(id=perm_id)[0]
        permission_list.append( (elem, perm) )
    return permission_list

KNOWN_PROJECT_FILTERS = (
    ('name',    'Name matches'),
    ('stacks', 'Same stacks linked and all new stacks are known'),
)

KNOWN_PROJECT_STRATEGIES = (
    ('add-anyway',     'Add imported project anyway'),
    ('ignore',         'Ignore imported project'),
    ('merge',          'Merge with existing project, add new stacks'),
    ('merge-override', 'Merge with existing project, override stacks'),
    ('replace',        'Replace existing projects with new version')
)

class DataFileForm(forms.Form):
    """ A form to select basic properties on the data to be
    imported. Path and filter constraints can be set here.
    """
    import_from = forms.ChoiceField(
            initial=settings.IMPORTER_DEFAULT_DATA_SOURCE,
            choices=(('filesystem', 'Data directory on server'),
                     ('remote-catmaid', 'Remote CATMAID instance'),
                     ('remote', 'General remote host')),
            help_text="Where new pojects and stacks will be looked for")
    relative_path = forms.CharField(required=False, widget=forms.TextInput(
        attrs={'size':'40', 'class': 'import-source-setting filesystem-import'}),
        help_text="Optionally, use a sub-folder of the data folder to narrow " \
                  "down the folders to look at. This path is <em>relative</em> " \
                  "to the data folder in use.")
    remote_host = forms.CharField(required=False, widget=forms.TextInput(
        attrs={'size':'40', 'class': 'import-source-setting remote-import'}),
        help_text="The URL to a remote host from which projects and stacks " \
                  "can be imported. To connect to another CATMAID server, add " \
                  "/projects/export to its URL.")
    catmaid_host = forms.CharField(required=False, widget=forms.TextInput(
        attrs={'size':'40', 'class': 'import-source-setting catmaid-host'}),
        help_text="The main URL of the remote CATMAID instance.")
    api_key = forms.CharField(required=False, widget=forms.TextInput(
        attrs={'size':'40', 'class': 'import-source-setting api-key'}),
        help_text="(Optional) API-Key of your user on the remote CATMAID instance.")
    http_auth_user = forms.CharField(required=False, widget=forms.TextInput(
        attrs={'size':'20', 'class': 'import-source-setting http-auth-user'}),
        help_text="(Optional) HTTP-Auth username for the remote server.")
    http_auth_pass = forms.CharField(required=False, widget=forms.PasswordInput(
        attrs={'size':'20', 'class': 'import-source-setting http-auth-user'}),
        help_text="(Optional) HTTP-Auth password for the remote server.")
    filter_term = forms.CharField(initial="*", required=False,
        widget=forms.TextInput(attrs={'size':'40'}),
        help_text="Optionally, you can apply a <em>glob filter</em> to the " \
                  "projects found in your data folder.")
    known_project_filter = forms.MultipleChoiceField(KNOWN_PROJECT_FILTERS,
            label='Projects are known if', initial=('name', 'stacks'),
            widget=forms.CheckboxSelectMultiple(), required=False,
            help_text='Select what makes makes a project known. An OR operation ' \
                    'will be used to combine multiple selections.')
    known_project_strategy = forms.ChoiceField(
            initial=getattr(settings, 'IMPORTER_DEFAULT_EXISTING_DATA_STRATEGY', 'merge'),
            choices=KNOWN_PROJECT_STRATEGIES,
            help_text="Decide if imported projects that are already known " \
                    "(see above) should be ignored, merged or replaced.")
    base_url = forms.CharField(required=True,
        widget=forms.TextInput(attrs={'size':'40'}),
        help_text="The <em>base URL</em> should give read access to the data \
                   folder in use.")

    def clean(self):
        form_data = self.cleaned_data

        # Make sure URLs are provided for a remote import
        import_from = form_data['import_from']
        if 'remote-catmaid' == import_from:
            if 0 == len(form_data['catmaid_host'].strip()):
                raise ValidationError(_('No URL provided'))
        elif 'remote' == import_from:
            if 0 == len(form_data['remote_host'].strip()):
                raise ValidationError(_('No URL provided'))

        return form_data

class ProjectSelectionForm(forms.Form):
    """ A form to select projects to import out of the
    available projects.
    """
    # A checkbox for each project, checked by default
    projects = forms.MultipleChoiceField(required=False,
        widget=forms.CheckboxSelectMultiple(
            attrs={'class': 'autoselectable'}),
        help_text="Only selected projects will be imported.")
    remove_unref_stack_data = forms.BooleanField(initial=False,
        required=False, label="Remove unreferenced stacks and stack groups",
         help_text="If checked, all stacks and stack groups that are not "
         "referenced after the import will be removed.")
    tags = forms.CharField(initial="", required=False,
        widget=forms.TextInput(attrs={'size':'50'}),
        help_text="A comma separated list of unquoted tags.")
    default_tile_width = forms.IntegerField(
        initial=settings.IMPORTER_DEFAULT_TILE_WIDTH,
        help_text="The default width of one tile in <em>pixel</em>, " \
            "used if not specified for a stack.")
    default_tile_height = forms.IntegerField(
        initial=settings.IMPORTER_DEFAULT_TILE_HEIGHT,
        help_text="The default height of one tile in <em>pixel</em>, " \
            "used if not specified for a stack.")
    default_tile_source_type = forms.ChoiceField(
            initial=settings.IMPORTER_DEFAULT_TILE_SOURCE_TYPE,
            choices=TILE_SOURCE_TYPES,
            help_text="The default tile source type is used if there " \
                    "none defined for an imported stack. It represents " \
                    "how the tile data is organized. See " \
                    "<a href=\"http://catmaid.org/page/tile_sources.html\">"\
                    "tile source conventions documentation</a>.")
    link_classifications = forms.BooleanField(initial=False,
        required=False, help_text="If checked, this option will " \
            "let the importer suggest classification graphs to " \
            "link the new projects against and will allow manual " \
            "links as well.")
    user_permissions = forms.MultipleChoiceField(required=False,
        widget=FilteredSelectMultiple('user permissions', is_stacked=False),
        help_text="The selected <em>user/permission combination</em> \
                   will be assigned to every project.")
    group_permissions = forms.MultipleChoiceField(required=False,
        widget=FilteredSelectMultiple('group permissions', is_stacked=False),
        help_text="The selected <em>group/permission combination</em> \
                   will be assigned to every project.")

def create_classification_linking_form():
    """ Create a new ClassificationLinkingForm instance.
    """
    workspace_pid = settings.ONTOLOGY_DUMMY_PROJECT_ID
    root_links = get_classification_links_qs( workspace_pid, [], True )
    # Make sure we use no classification graph more than once
    known_roots = []
    root_ids = []
    for link in root_links:
        if link.class_instance_b.id not in known_roots:
            known_roots.append(link.class_instance_b.id)
            root_ids.append(link.id)

    class ClassificationLinkingForm(forms.Form):
        # A check-box for each project, checked by default
        classification_graph_suggestions = forms.MultipleChoiceField(required=False,
            widget=forms.CheckboxSelectMultiple(),
            help_text="Only selected classification graphs will be linked to the new projects.")
        additional_links = forms.ModelMultipleChoiceField(required=False,
            widget=FilteredSelectMultiple('Classification roots',
                is_stacked=False),
            queryset = ClassInstanceClassInstanceProxy.objects.filter(id__in=root_ids))

    return ClassificationLinkingForm

class ConfirmationForm(forms.Form):
    """ A form to confirm the selection and settings of the
    projects that are about to be imported.
    """
    something = forms.CharField(initial="", required=False)

def import_projects( user, pre_projects, tags, permissions,
        default_tile_width, default_tile_height, default_tile_source_type,
        cls_graph_ids_to_link, remove_unref_stack_data):
    """ Creates real CATMAID ojects out of the PreProject objects
    and imports them into CATMAID.
    """
    remove_unimported_linked_stacks = False
    known_stack_action = None
    known_stackgroup_action = None
    imported = []
    not_imported = []
    cursor = connection.cursor()
    for pp in pre_projects:
        project = None
        currently_linked_stacks = []
        currently_linked_stack_ids = []
        links = {}
        all_stacks_known = all(s.already_known for s in pp.stacks)
        try:
            if pp.already_known:
                project = Project.objects.get(pk=pp.already_known_pid)
                currently_linked_stacks = [ps.stack for ps in \
                        ProjectStack.objects.filter(project=project)]
                currently_linked_stack_ids = [s.id for s in \
                        currently_linked_stacks]

                # Check if all imported stacks are already linked to the
                # exisiting project. For now onlt consider a stack linked if the
                # existing project is the only link.
                links = {ss:[l for l in currently_linked_stacks if ss.equals(l)] \
                        for ss in pp.stacks}
                all_stacks_linked = all(len(l) == 1 for l in links.values())

                if 'ignore' == pp.action:
                    # Ignore all projects that are marked to be ignored
                    continue
                elif 'add-anyway' == pp.action:
                    project = None
                    known_stack_action = 'import'
                    known_stackgroup_action = 'import'
                elif 'merge' == pp.action:
                    # If all stacks are known already and linked to the existing
                    # project, there is nothing left to do here. Otherwise, ignore
                    # existing stacks.
                    if all_stacks_linked:
                      continue
                    # Merging projects means adding new stacks to an existing
                    # project, ignoring all existing linked stacks.
                    known_stack_action = 'ignore'
                    known_stackgroup_action = 'merge'
                    remove_unimported_linked_stacks = False
                elif 'merge-override' == pp.action:
                    # Re-use existing project, but override properties of
                    # existing stacks and remove links that are not part of the
                    # new project definition
                    known_stack_action = 'override'
                    known_stackgroup_action = 'override'
                    remove_unimported_linked_stacks = True
                elif 'replace' == pp.action:
                    # Delete existg projects and import all stacks
                    project.delete()
                    project = None
                    known_stack_action = 'import'
                    known_stackgroup_action = 'import'
                    remove_unimported_linked_stacks = False
                else:
                    raise ValueError('Invalid known project action: ' + pp.action)

                if remove_unimported_linked_stacks:
                    # Remove all existing links that don't link to existing
                    # projects.
                    for stack in currently_linked_stacks:
                        if not pp.imports_stack(stack):
                            # Delete project stack entry and stack group membership.
                            stack.delete()

            # Create stacks and add them to project
            stacks = []
            updated_stacks = []
            stack_groups = {}
            translations = {}
            for s in pp.stacks:

                # Test if stack is alrady known. This can change with every
                # iteration. At least if we want to re-use stacks defined
                # multiple times in import. Maybe make this an option?
                known_before = s.already_known
                linked_objects = links.get(s) or []
                valid_link = len(linked_objects) == 1
                existing_stack = linked_objects[0] if valid_link else None

                stack_properties = {
                    'title': s.name,
                    'dimension': s.dimension,
                    'resolution': s.resolution,
                    'image_base': s.image_base,
                    'num_zoom_levels': s.num_zoom_levels,
                    'file_extension': s.file_extension,
                    'tile_width': getattr(s, "tile_width", default_tile_width),
                    'tile_height': getattr(s, "tile_height", default_tile_height),
                    'tile_source_type': getattr(s, "tile_source_type",
                        default_tile_source_type),
                    'metadata': s.metadata
                }

                stack = None

                if valid_link:
                  if 'ignore' ==  known_stack_action:
                      continue
                  elif 'import' == known_stack_action:
                      # Nothing to do, just for completeness
                      pass
                  elif 'override' == known_stack_action:
                      # Copy properties of known imported stacks to matching
                      # existing ones that have a valid link to the existing
                      # project.
                      stack = existing_stack
                      for k,v in stack_properties.iteritems():
                        if hasattr(stack, k):
                            setattr(stack, k, v)
                        else:
                            raise ValueError("Unknown stack field: " + k)
                      stack.save()
                      updated_stacks.append(stack)
                  else:
                      raise ValueError("Invalid action for known stacks: " +
                          str(known_stack_action))

                # TODO This breaks if the same stack is imported multiple times
                # into the same imported project. Maybe we shouldn't only
                # consider a single linked stack as valid.
                if not stack:
                    stack = Stack.objects.create(**stack_properties)
                    stacks.append(stack)

                # Add overlays of this stack
                for o in s.overlays:

                    overlay_properties = {
                        'title': o.name,
                        'stack': stack,
                        'image_base': o.image_base,
                        'default_opacity': o.default_opacity,
                        'file_extension': o.file_extension,
                        'tile_width': getattr(o, "tile_width", default_tile_width),
                        'tile_height': getattr(o, "tile_height", default_tile_height),
                        'tile_source_type': getattr(o, "tile_source_type",
                            default_tile_source_type)
                    }

                    overlay = None
                    known_overlay = None
                    if existing_stack:
                        known_overlay = Overlay.objects.filter(
                            image_base=o.image_base, stack=existing_stack)
                    if len(known_overlays) > 0:
                      if 'ignore' == known_stack_action:
                          continue
                      elif 'import' == known_stack_action:
                          pass
                      elif 'override' == known_stack_action:
                          # Find a linked (!) and matching overlay
                          overlay = existing_overlay
                          for k,v in overlay_properties.iteritems():
                            if hasattr(overlay, k):
                                setattr(ovrelay, k, v)
                            else:
                                raise ValueError("Unknown stack field: " + k)
                          overlay.save()
                      else:
                          raise ValueError("Invalid action for known stacks: " +
                              known_stack_action)

                      # Default to overlay creation
                      if not overlay:
                         overlay = Overlay.objects.create(**overlay_properties)

                # Collect stack group information
                for sg in s.stackgroups:
                    stack_group = stack_groups.get(sg.name)
                    if not stack_group:
                        stack_group = []
                        stack_groups[sg.name] = stack_group
                    stack_group.append({
                        'stack': stack,
                        'relation': sg.relation
                    })
                # Keep track of project-stack offsets
                translations[stack] = s.project_translation

            # Create new project, if no existing one has been selected before
            p = project or Project.objects.create(title=pp.name)
            # Assign permissions to project
            assigned_permissions = []
            for user_or_group, perm in permissions:
                assigned_perm = assign( perm.codename, user_or_group, p )
                assigned_permissions.append( assigned_perm )
            # Tag the project
            p.tags.add( *tags )
            # Add stacks to import to project
            for s in stacks:
                trln = Double3D.from_str(translations[s])
                ps = ProjectStack.objects.create(
                    project=p, stack=s, translation=trln)
            # Update project links of updated projects
            for us in updated_stacks:
                trln = Double3D.from_str(translations[us])
                ps = ProjectStack.objects.create(project=p, stack=us,
                    translation=trln)
            # Make project changes persistent
            p.save()

            # Save stack groups. If
            for sg, linked_stacks in stack_groups.iteritems():
                existing_stackgroups = ClassInstance.objects.filter(project=p,
                    class_column__class_name="stackgroup")
                
                if len(existing_stackgroups) > 0:
                    if 'ignore' == known_stackgroup_action:
                        continue
                    elif 'import' == known_stackgroup_action:
                        pass
                    elif 'merge' == known_stackgroup_action:
                        pass
                    elif 'override' == known_stackgroup_action:
                        existing_stackgroups.delete()
                
                stack_group = ClassInstance.objects.create(
                    user=user, project=p, name=sg,
                    class_column=Class.objects.get(project=p, class_name="stackgroup")
                )
                for ls in linked_stacks:
                    StackClassInstance.objects.create(
                        user=user, project=p,
                        relation=Relation.objects.get(project=p,
                            relation_name=ls['relation']),
                        class_instance=stack_group,
                        stack=ls['stack'],
                    )

            # Link classification graphs
            for cg in cls_graph_ids_to_link:
                workspace = settings.ONTOLOGY_DUMMY_PROJECT_ID
                cgroot = ClassInstance.objects.get(pk=cg)
                link_existing_classification(workspace, user, p, cgroot)
            # Remember created project
            imported.append( pp )

            # If unrefernced stacks and implicitely unreferenced stack groups
            # should be removed, find all of them and and remove them in one go.
            if remove_unref_stack_data:
              cursor.execute("""
                  SELECT s.id
                  FROM  stack s
                  LEFT OUTER JOIN project_stack ps
                    ON s.id = ps.stack_id
                  WHERE
                    ps.id IS NULL
              """)
              unused_stack_ids = [r[0] for r in cursor.fetchall()]
              # Delete cascaded with the help of Django
              Stack.objects.filter(id__in=unused_stack_ids).delete()
              # Delete all empty stack groups
              cursor.execute("""
                  DELETE FROM class_instance
                  USING class_instance ci
                  LEFT OUTER JOIN stack_class_instance sci
                    ON ci.id = sci.class_instance_id
                  JOIN class c
                    ON ci.class_id = c.id
                  WHERE sci.class_instance_id IS NULL
                    AND c.class_name = 'stackgroup'
              """)

        except Exception as e:
            import traceback
            not_imported.append((
                pp,
                Exception("Couldn't import project: {} {}".format(str(e),
                        str(traceback.format_exc())))
            ))

    return (imported, not_imported)
