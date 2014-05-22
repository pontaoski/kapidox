# -*- coding: utf-8 -*-
#
# Copyright 2014  Alex Merry <alex.merry@kdemail.net>
# Copyright 2014  Aurélien Gâteau <agateau@kde.org>
# Copyright 2014  Alex Turbov <i.zaufi@gmail.com>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES
# OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
# IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT
# NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF
# THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

# Python 2/3 compatibility (NB: we require at least 2.7)
from __future__ import division, absolute_import, print_function, unicode_literals

import argparse
import codecs
import datetime
import os
import logging
import shutil
import subprocess
import sys

from fnmatch import fnmatch
try:
    from urllib.parse import urljoin
except ImportError:
    from urlparse import urljoin

import jinja2

from kapidox import utils
from .doxyfilewriter import DoxyfileWriter

__all__ = (
    "Context",
    "create_arg_parser",
    "copy_dir_contents",
    "find_doxdatadir_or_exit",
    "generate_apidocs",
    "load_template",
    "search_for_tagfiles",
    "WARN_LOGFILE",
    "build_classmap",
    "postprocess",
    "create_dirs",
    "write_mapping_to_php",
    )

WARN_LOGFILE = 'doxygen-warnings.log'

HTML_SUBDIR = 'html'


class Context(object):
    """
    Holds parameters used by the various functions of the generator
    """
    __slots__ = (
        # Names
        'modulename',
        'fancyname',
        'title',
        # Input
        'srcdir',
        'doxdatadir',
        'tagfiles',
        'resourcedir',
        'dependency_diagram',
        # Output
        'outputdir',
        'htmldir',
        'tagfile',
        # Output options
        'man_pages',
        'qhp',
        'searchengine',
        'api_searchbox',
        # Binaries
        'doxygen',
        'qhelpgenerator',
    )

    def __init__(self, args, **kwargs):
        self.title = args.title
        # Output options
        self.man_pages = args.man_pages
        self.qhp = args.qhp
        self.searchengine = args.searchengine
        self.api_searchbox = args.api_searchbox
        # Binaries
        self.doxygen = args.doxygen
        self.qhelpgenerator = args.qhelpgenerator

        for key in self.__slots__:
            if not hasattr(self, key):
                setattr(self, key, kwargs.get(key))


def create_arg_parser(**kwargs):
    parser = argparse.ArgumentParser(**kwargs)
    parser.add_argument('--title', default='KDE API Documentation',
            help='String to use for page titles.')
    parser.add_argument('--man-pages', action='store_true',
            help='Generate man page documentation.')
    parser.add_argument('--qhp', action='store_true',
            help='Generate Qt Compressed Help documentation.')
    parser.add_argument('--searchengine', action='store_true',
            help="Enable Doxygen's search engine feature.")
    parser.add_argument('--api-searchbox', action='store_true',
            help="Enable the API searchbox (only useful for api.kde.org).")
    parser.add_argument('--doxdatadir',
            help='Location of the HTML header files and support graphics.')
    parser.add_argument('--qtdoc-dir',
            help='Location of (local) Qt documentation; this is searched ' +
                 'for tag files to create links to Qt classes.')
    parser.add_argument('--qtdoc-link',
            help='Override Qt documentation location for the links in the ' +
                 'html files.  May be a path or URL.')
    parser.add_argument('--qtdoc-flatten-links', action='store_true',
            help='Whether to assume all Qt documentation html files ' +
                 'are immediately under QTDOC_LINK (useful if you set ' +
                 'QTDOC_LINK to the online Qt documentation).  Ignored ' +
                 'if QTDOC_LINK is not set.')
    parser.add_argument('--doxygen', default='doxygen',
            help='(Path to) the doxygen executable.')
    parser.add_argument('--qhelpgenerator', default='qhelpgenerator',
            help='(Path to) the qhelpgenerator executable.')
    parser.add_argument('--keep-temp-dirs', action='store_true',
            help='Do not delete temporary dirs, useful for debugging.')
    return parser


def create_dirs(ctx):
    ctx.htmldir = os.path.join(ctx.outputdir, HTML_SUBDIR)
    ctx.tagfile = os.path.join(ctx.htmldir, ctx.modulename + '.tags')

    if not os.path.exists(ctx.outputdir):
        os.makedirs(ctx.outputdir)
    if not os.path.exists(ctx.htmldir):
        os.makedirs(ctx.htmldir)

    if ctx.resourcedir is None:
        copy_dir_contents(os.path.join(ctx.doxdatadir, 'htmlresource'), ctx.htmldir)
        ctx.resourcedir = '.'


def load_template(path):
    # Set errors to 'ignore' because we don't want weird characters in Doxygen
    # output (for example source code listing) to cause a failure
    content = codecs.open(path, encoding='utf-8', errors='ignore').read()
    try:
        return jinja2.Template(content)
    except jinja2.exceptions.TemplateSyntaxError as exc:
        logging.error('Failed to parse template {}'.format(path))
        raise


def smartjoin(pathorurl1,*args):
    """Join paths or URLS

    It figures out which it is from whether the first contains a "://"
    """
    if '://' in pathorurl1:
        if not pathorurl1.endswith('/'):
            pathorurl1 += '/'
        return urljoin(pathorurl1,*args)
    else:
        return os.path.join(pathorurl1,*args)

def find_datadir(searchpaths, testentries, suggestion=None, complain=True):
    """Find data files

    Looks at suggestion and the elements of searchpaths to see if any of them
    is a directory that contains the entries listed in testentries.

    searchpaths -- directories to test
    testfiles   -- files to check for in the directory
    suggestion  -- the first place to look
    complain    -- print a warning if suggestion is not correct

    Returns a path to a directory containing everything in testentries, or None
    """

    def check_datadir_entries(directory):
        """Check for the existence of entries in a directory"""
        for e in testentries:
            if not os.path.exists(os.path.join(directory,e)):
                return False
        return True

    if not suggestion is None:
        if not os.path.isdir(suggestion):
            logging.warning(suggestion + " is not a directory")
        elif not check_datadir_entries(suggestion):
            logging.warning(suggestion + " does not contain the expected files")
        else:
            return suggestion


    for d in searchpaths:
        d = os.path.realpath(d)
        if os.path.isdir(d) and check_datadir_entries(d):
            return d

    return None

def find_tagfiles(docdir, doclink=None, flattenlinks=False, _depth=0):
    """Find Doxygen-generated tag files in a directory

    The tag files must have the extention .tags, and must be in the listed
    directory, a subdirectory or a subdirectory named html of a subdirectory.

    docdir       -- the directory to search
    doclink      -- the path or URL to use when creating the documentation
                    links; if None, this will default to docdir
    flattenlinks -- if this is True, generated links will assume all the html
                    files are directly under doclink; if False (the default),
                    the html files are assumed to be at the same relative
                    location to doclink as the tag file is to docdir; ignored
                    if doclink is not set

    Returns a list of pairs of (tag_file,link_path)
    """

    if not os.path.isdir(docdir):
        return []

    if doclink is None:
        doclink = docdir
        flattenlinks = False

    def nestedlink(subdir):
        if flattenlinks:
            return doclink
        else:
            return smartjoin(doclink,subdir)

    tagfiles = []

    entries = os.listdir(docdir)
    for e in entries:
        path = os.path.join(docdir,e)
        if os.path.isfile(path) and e.endswith('.tags'):
            tagfiles.append((path,doclink))
        elif (_depth == 0 or (_depth == 1 and e == 'html')) and os.path.isdir(path):
            tagfiles += find_tagfiles(path, nestedlink(e),
                          flattenlinks=flattenlinks, _depth=_depth+1)

    return tagfiles

def search_for_tagfiles(suggestion=None, doclink=None, flattenlinks=False, searchpaths=[]):
    """Find Doxygen-generated tag files

    See the find_tagfiles documentation for how the search is carried out in
    each directory; this just allows a list of directories to be searched.

    At least one of docdir or searchpaths must be given for it to find anything.

    suggestion   -- the first place to look (will complain if there are no
                    documentation tag files there)
    doclink      -- the path or URL to use when creating the documentation
                    links; if None, this will default to docdir
    flattenlinks -- if this is True, generated links will assume all the html
                    files are directly under doclink; if False (the default),
                    the html files are assumed to be at the same relative
                    location to doclink as the tag file is to docdir; ignored
                    if doclink is not set
    searchpaths  -- other places to look for documentation tag files

    Returns a list of pairs of (tag_file,link_path)
    """

    if not suggestion is None:
        if not os.path.isdir(suggestion):
            logging.warning(suggestion + " is not a directory")
        else:
            tagfiles = find_tagfiles(suggestion, doclink, flattenlinks)
            if len(tagfiles) == 0:
                logging.warning(suggestion + " does not contain any tag files")
            else:
                return tagfiles

    for d in searchpaths:
        tagfiles = find_tagfiles(d, doclink, flattenlinks)
        if len(tagfiles) > 0:
            logging.info("Documentation tag files found at " + d)
            return tagfiles

    return []

def copy_dir_contents(directory, dest):
    """Copy the contents of a directory

    directory -- the directory to copy the contents of
    dest      -- the directory to copy them into
    """
    ignored = ['CMakeLists.txt']
    ignore = shutil.ignore_patterns(*ignored)
    logging.info("Copying contents of " + directory)
    for fn in os.listdir(directory):
        f = os.path.join(directory,fn)
        if os.path.isfile(f):
            docopy = True
            for i in ignored:
                if fnmatch(fn,i):
                    docopy = False
                    break
            if docopy:
                shutil.copy(f,dest)
        elif os.path.isdir(f):
            dest_f = os.path.join(dest,fn)
            if os.path.isdir(dest_f):
                shutil.rmtree(dest_f)
            shutil.copytree(f, dest_f, ignore=ignore)

def find_doxdatadir_or_exit(suggestion):
    """Finds the common documentation data directory

    Exits if not found.
    """
    # FIXME: use setuptools and pkg_resources?
    scriptdir = os.path.dirname(os.path.realpath(__file__))
    doxdatadir = find_datadir(
            searchpaths=[os.path.join(scriptdir,'data')],
            testentries=['header.html','footer.html','htmlresource'],
            suggestion=suggestion)
    if doxdatadir is None:
        logging.error("Could not find a valid doxdatadir")
        sys.exit(1)
    else:
        logging.info("Found doxdatadir at " + doxdatadir)
    return doxdatadir


def menu_items(htmldir, modulename):
    """Menu items for standard Doxygen files

    Looks for a set of standard Doxygen files (like namespaces.html) and
    provides menu text for those it finds in htmldir.

    htmldir -- the directory the HTML files are contained in

    Returns a list of maps with 'text' and 'href' keys
    """
    entries = [
            {'text': 'Main Page', 'href': 'index.html'},
            {'text': 'Namespace List', 'href': 'namespaces.html'},
            {'text': 'Namespace Members', 'href': 'namespacemembers.html'},
            {'text': 'Alphabetical List', 'href': 'classes.html'},
            {'text': 'Class List', 'href': 'annotated.html'},
            {'text': 'Class Hierarchy', 'href': 'hierarchy.html'},
            {'text': 'File List', 'href': 'files.html'},
            {'text': 'File Members', 'href': 'globals.html'},
            {'text': 'Modules', 'href': 'modules.html'},
            {'text': 'Directories', 'href': 'dirs.html'},
            {'text': 'Dependencies', 'href': modulename + '-dependencies.html'},
            {'text': 'Related Pages', 'href': 'pages.html'},
            ]
    # NOTE In Python 3 filter() builtin returns an iterable, but not a list
    #      type, so explicit conversion is here!
    return list(filter(
            lambda e: os.path.isfile(os.path.join(htmldir, e['href'])),
            entries))

def postprocess_internal(htmldir, mapping):
    """Substitute text in HTML files

    Performs text substitutions on each line in each .html file in a directory.

    htmldir -- the directory containing the .html files
    mapping -- a dict of mappings
    """
    for f in os.listdir(htmldir):
        if f.endswith('.html'):
            path = os.path.join(htmldir,f)
            newpath = path + '.new'
            try:
                with codecs.open(newpath, 'w', 'utf-8') as outf:
                    tmpl = load_template(path)
                    outf.write(tmpl.render(mapping))
                os.rename(newpath, path)
            except Exception:
                logging.error('postprocessing {} failed'.format(path))
                raise

def build_classmap(tagfile):
    """Parses a tagfile to get a map from classes to files

    tagfile -- the Doxygen-generated tagfile to parse

    Returns a list of maps (keys: classname and filename)
    """
    import xml.etree.ElementTree as ET
    tree = ET.parse(tagfile)
    tagfile_root = tree.getroot()
    mapping = []
    for compound in tagfile_root:
        kind = compound.get('kind')
        if kind == 'class' or kind == 'namespace':
            name_el = compound.find('name')
            filename_el = compound.find('filename')
            mapping.append({'classname': name_el.text,
                            'filename': filename_el.text})
    return mapping

def write_mapping_to_php(mapping, outputfile, varname='map'):
    """Write a mapping out as PHP code

    Creates a PHP array as described by mapping.  For example, the mapping
      [("foo","bar"),("x","y")]
    would cause the file
      <?php $map = array('foo' => 'bar','x' => 'y') ?>
    to be written out.

    mapping    -- a list of pairs of strings
    outputfile -- the file to write to
    varname    -- override the PHP variable name (defaults to 'map')
    """
    logging.info('Generating PHP mapping')
    with codecs.open(outputfile,'w','utf-8') as f:
        f.write('<?php $' + varname + ' = array(')
        first = True
        for entry in mapping:
            if first:
                first = False
            else:
                f.write(',')
            f.write("'" + entry['classname'] + "' => '" + entry['filename'] + "'")
        f.write(') ?>')

def generate_dependencies_page(tmp_dir, doxdatadir, modulename, dependency_diagram):
    """Create `modulename`-dependencies.md in `tmp_dir`"""
    template_path = os.path.join(doxdatadir, 'dependencies.md.tmpl')
    out_path = os.path.join(tmp_dir, modulename + '-dependencies.md')
    tmpl = load_template(template_path)
    with codecs.open(out_path, 'w', 'utf-8') as outf:
        txt = tmpl.render({
                'modulename': modulename,
                'diagramname': os.path.basename(dependency_diagram),
                })
        outf.write(txt)
    return out_path

def generate_apidocs(ctx, tmp_dir, doxyfile_entries=None, keep_temp_dirs=False):
    """Generate the API documentation for a single directory"""

    def find_src_subdir(d):
        pth = os.path.join(ctx.srcdir, d)
        if os.path.isdir(pth):
            return [pth]
        else:
            return []

    # Paths and basic project info

    # FIXME: preprocessing?
    # What about providing the build directory? We could get the version
    # as well, then.

    input_list = [ctx.srcdir]
    image_path_list = []

    if ctx.dependency_diagram:
        input_list.append(generate_dependencies_page(tmp_dir, ctx.doxdatadir, ctx.modulename, ctx.dependency_diagram))
        image_path_list.append(ctx.dependency_diagram)

    doxyfile_path = os.path.join(tmp_dir, 'Doxyfile')
    with codecs.open(doxyfile_path, 'w', 'utf-8') as doxyfile:

        # Global defaults
        with codecs.open(os.path.join(ctx.doxdatadir,'Doxyfile.global'), 'r', 'utf-8') as f:
            for line in f:
                doxyfile.write(line)

        writer = DoxyfileWriter(doxyfile)
        writer.write_entry('PROJECT_NAME', ctx.fancyname)
        # FIXME: can we get the project version from CMake?

        # Input locations
        image_path_list.extend(find_src_subdir('docs/pics'))
        writer.write_entries(
                INPUT=input_list,
                DOTFILE_DIRS=find_src_subdir('docs/dot'),
                EXAMPLE_PATH=find_src_subdir('docs/examples'),
                IMAGE_PATH=image_path_list)

        # Other input settings
        writer.write_entry('TAGFILES', [f + '=' + loc for f, loc in ctx.tagfiles])

        # Output locations
        writer.write_entries(
                OUTPUT_DIRECTORY=ctx.outputdir,
                GENERATE_TAGFILE=ctx.tagfile,
                HTML_OUTPUT=HTML_SUBDIR,
                WARN_LOGFILE=os.path.join(ctx.outputdir, WARN_LOGFILE))

        # Other output settings
        writer.write_entries(
                HTML_HEADER=ctx.doxdatadir + '/header.html',
                HTML_FOOTER=ctx.doxdatadir + '/footer.html'
                )

        # Always write these, even if QHP is disabled, in case Doxygen.local
        # overrides it
        writer.write_entries(
                QHP_VIRTUAL_FOLDER=ctx.modulename,
                QHG_LOCATION=ctx.qhelpgenerator)

        writer.write_entries(
                GENERATE_MAN=ctx.man_pages,
                GENERATE_QHP=ctx.qhp,
                SEARCHENGINE=ctx.searchengine)

        if doxyfile_entries:
            writer.write_entries(**doxyfile_entries)

        # Module-specific overrides
        localdoxyfile = os.path.join(ctx.srcdir, 'docs/Doxyfile.local')
        if os.path.isfile(localdoxyfile):
            with codecs.open(localdoxyfile, 'r', 'utf-8') as f:
                for line in f:
                    doxyfile.write(line)

    logging.info('Running Doxygen')
    subprocess.call([ctx.doxygen, doxyfile_path])


def postprocess(ctx, classmap, template_mapping=None):
    copyright = '1996-' + str(datetime.date.today().year) + ' The KDE developers'
    mapping = {
            'resources': ctx.resourcedir,
            'title': ctx.title,
            'copyright': copyright,
            'api_searchbox': ctx.api_searchbox,
            'doxygen_menu': {'entries': menu_items(ctx.htmldir, ctx.modulename)},
            'class_map': {'classes': classmap},
            'kapidox_version': utils.get_kapidox_version(),
        }
    if template_mapping:
        mapping.update(template_mapping)
    logging.info('Postprocessing')
    postprocess_internal(ctx.htmldir, mapping)
