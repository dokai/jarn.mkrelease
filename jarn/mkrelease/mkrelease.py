import locale
locale.setlocale(locale.LC_ALL, '')

import pkg_resources
__version__ = pkg_resources.get_distribution('jarn.mkrelease').version

import sys
import os
import getopt
import tempfile
import shutil

from os.path import abspath, join, expanduser, exists, isfile
from itertools import chain

from python import Python
from setuptools import Setuptools
from scp import SCP
from scm import SCMFactory
from urlparser import URLParser
from configparser import ConfigParser
from exit import err_exit, msg_exit, warn

MAXALIASDEPTH = 23

VERSION = "jarn.mkrelease %s" % __version__
USAGE = "Try 'mkrelease --help' for more information"

HELP = """\
Usage: mkrelease [options] [scm-url [rev]|scm-sandbox]

Python egg releaser

Options:
  -C, --no-commit     Do not commit modified files from the sandbox.
  -T, --no-tag        Do not tag the release in SCM.
  -S, --no-upload     Do not upload the release to dist-location.
  -n, --dry-run       Dry-run; equivalent to -CTS.

  --svn, --hg, --git  Select the SCM type. Only required if the SCM type
                      cannot be guessed from the argument.

  -d dist-location, --dist-location=dist-location
                      An scp or sftp destination specification, an index
                      server configured in ~/.pypirc, or an alias name for
                      either. This option may be specified more than once.

  -s, --sign          Sign the release with GnuPG.
  -i identity, --identity=identity
                      The GnuPG identity to sign with.

  -p, --push          Push sandbox modifications upstream.
  -e, --develop       Allow version number extensions.
  -b, --binary        Release a binary egg.
  -q, --quiet         Suppress output of setuptools commands.

  -c config-file, --config-file=config-file
                      Use config-file instead of the default ~/.mkrelease.

  -l, --list-locations
                      List known dist-locations and exit.
  -h, --help          Print this help message and exit.
  -v, --version       Print the version string and exit.

  scm-url             The URL of a remote SCM repository. The rev argument
                      specifies a branch or tag to check out.
  scm-sandbox         A local SCM sandbox. Defaults to the current working
                      directory.
"""


class Defaults(object):

    def __init__(self, config_file):
        """Read config files.
        """
        parser = ConfigParser(warn)
        parser.read((expanduser('~/.pypirc'), config_file))

        main_section = 'mkrelease'
        if not parser.has_section(main_section) and parser.has_section('defaults'):
            main_section = 'defaults' # BBB

        self.distbase = parser.getstring(main_section, 'distbase', '')
        self.distdefault = parser.getlist(main_section, 'distdefault', [])

        self.sign = parser.getboolean(main_section, 'sign', False)
        self.identity = parser.getstring(main_section, 'identity', '')
        self.push = parser.getboolean(main_section, 'push', False)

        self.aliases = {}
        if parser.has_section('aliases'):
            for key, value in parser.items('aliases'):
                self.aliases[key] = parser.to_list(value)

        class ServerInfo(object):
            def __init__(self, server):
                self.sign = parser.getboolean(server, 'sign', None)
                self.identity = parser.getstring(server, 'identity', None)

        self.servers = {}
        for server in parser.getlist('distutils', 'index-servers', []):
            self.servers[server] = ServerInfo(server)

    def get_known_locations(self):
        """Return a set of known locations.
        """
        return set(chain(self.aliases, self.servers))


class Locations(object):

    def __init__(self, defaults):
        self.distbase = defaults.distbase
        self.distdefault = defaults.distdefault
        self.aliases = defaults.aliases
        self.servers = defaults.servers
        self.locations = []
        self.urlparser = URLParser()

    def __len__(self):
        """Return number of locations.
        """
        return len(self.locations)

    def __iter__(self):
        """Iterate over locations.
        """
        return iter(self.locations)

    def extend(self, location):
        """Extend list of locations.
        """
        self.locations.extend(location)

    def is_server(self, location):
        """Return True if 'location' is an index server.
        """
        return location in self.servers

    def is_dist_url(self, location):
        """Return True if 'location' is an scp or sftp URL.
        """
        return self.urlparser.get_scheme(location) in ('scp', 'sftp')

    def has_host(self, location):
        """Return True if 'location' contains a host part.
        """
        return self.urlparser.is_ssh_url(location)

    def join(self, distbase, location):
        """Join 'distbase' and 'location' in such way that the
        result is a valid scp destination.
        """
        sep = ''
        if distbase and distbase[-1] not in (':', '/'):
            sep = '/'
        return distbase + sep + location

    def get_location(self, location, depth=0):
        """Resolve aliases and apply distbase.
        """
        if not location:
            return []
        if location in self.aliases:
            res = []
            if depth > MAXALIASDEPTH:
                err_exit('Maximum alias depth exceeded: %(location)s' % locals())
            for loc in self.aliases[location]:
                res.extend(self.get_location(loc, depth+1))
            return res
        if self.is_server(location):
            return [location]
        if location == 'pypi':
            err_exit('No configuration found for server: pypi\n'
                     'Please create a ~/.pypirc file')
        if self.urlparser.is_url(location):
            return [location]
        if not self.has_host(location) and self.distbase:
            return [self.join(self.distbase, location)]
        return [location]

    def get_default_location(self):
        """Return the default location.
        """
        res = []
        for location in self.distdefault:
            res.extend(self.get_location(location))
        return res

    def check_valid_locations(self, locations=None):
        """Fail if 'locations' is empty or contains bad destinations.
        """
        if locations is None:
            locations = self.locations
        if not locations:
            err_exit('mkrelease: option -d is required\n%s' % USAGE)
        for location in locations:
            if (not self.is_server(location) and
                not self.is_dist_url(location) and
                not self.has_host(location)):
                err_exit('Unknown location: %(location)s' % locals())


class ReleaseMaker(object):

    def __init__(self, args):
        """Initialize.
        """
        self.args = args
        self.set_defaults(expanduser('~/.mkrelease'))

    def set_defaults(self, config_file):
        """Set defaults.
        """
        self.defaults = Defaults(config_file)
        self.locations = Locations(self.defaults)
        self.python = Python()
        self.setuptools = Setuptools()
        self.scp = SCP()
        self.scms = SCMFactory()
        self.urlparser = URLParser()
        self.skipcommit = False
        self.skiptag = False
        self.skipupload = False
        self.push = self.defaults.push
        self.quiet = False
        self.sign = False
        self.list = False
        self.identity = ''
        self.branch = ''
        self.scmtype = ''
        self.distcmd = 'sdist'
        self.infoflags = ['--no-svn-revision', '--no-date', '--tag-build=""']
        self.distflags = ['--formats="zip"']
        self.directory = os.curdir
        self.scm = None

    def parse_options(self, args, depth=0):
        """Parse command line options.
        """
        try:
            options, remaining_args = getopt.gnu_getopt(args,
                'CSTbc:d:ehi:lnpqsv',
                ('no-commit', 'no-tag', 'no-upload', 'dry-run',
                 'sign', 'identity=', 'dist-location=', 'version', 'help',
                 'push', 'quiet', 'svn', 'hg', 'git', 'develop', 'binary',
                 'list-locations', 'config-file='))
        except getopt.GetoptError, e:
            err_exit('mkrelease: %s\n%s' % (e.msg, USAGE))

        for name, value in options:
            if name in ('-C', '--no-commit'):
                self.skipcommit = True
            elif name in ('-T', '--no-tag'):
                self.skiptag = True
            elif name in ('-S', '--no-upload'):
                self.skipupload = True
            elif name in ('-n', '--dry-run'):
                self.skipcommit = self.skiptag = self.skipupload = True
            elif name in ('-p', '--push'):
                self.push = True
            elif name in ('-q', '--quiet'):
                self.quiet = True
            elif name in ('-s', '--sign'):
                self.sign = True
            elif name in ('-i', '--identity'):
                self.identity = value
            elif name in ('-d', '--dist-location'):
                self.locations.extend(self.locations.get_location(value))
            elif name in ('-l', '--list-locations'):
                self.list = True
            elif name in ('-h', '--help'):
                msg_exit(HELP)
            elif name in ('-v', '--version'):
                msg_exit(VERSION)
            elif name in ('--svn', '--hg', '--git'):
                self.scmtype = name[2:]
            elif name in ('-e', '--develop'):
                self.skiptag = True
                self.infoflags = []
            elif name in ('-b', '--binary'):
                self.distcmd = 'bdist'
                self.distflags = ['--formats="egg"']
            elif name in ('-c', '--config-file') and depth == 0:
                config_file = abspath(expanduser(value))
                self.check_valid_file(config_file)
                self.set_defaults(config_file)
                return self.parse_options(args, depth+1)

        return remaining_args

    def list_locations(self):
        """Print known dist-locations and exit.
        """
        known = self.defaults.get_known_locations()
        for default in self.defaults.distdefault:
            if default not in known:
                known.add(default)
        for location in sorted(known):
            if location in self.defaults.distdefault:
                print location, '(default)'
            else:
                print location
        sys.exit(0)

    def check_valid_file(self, file):
        """Check if 'file' can be read.
        """
        if not exists(file):
            err_exit('No such file: %(file)s' % locals())
        if not isfile(file):
            err_exit('Not a file: %(file)s' % locals())
        if not os.access(file, os.R_OK):
            err_exit('File cannot be read: %(file)s' % locals())

    def get_uploadflags(self, location):
        """Return uploadflags for the given server.
        """
        uploadflags = []
        server = self.defaults.servers[location]

        if self.sign:
            if server.sign is None or server.sign:
                uploadflags.append('--sign')
        elif server.sign is not None:
            if server.sign:
                uploadflags.append('--sign')
        elif self.defaults.sign:
            uploadflags.append('--sign')

        if self.identity:
            if '--sign' not in uploadflags:
                uploadflags.append('--sign')
            uploadflags.append('--identity="%s"' % self.identity)
        elif '--sign' in uploadflags:
            if server.identity is not None:
                if server.identity:
                    uploadflags.append('--identity="%s"' % server.identity)
            elif self.defaults.identity:
                uploadflags.append('--identity="%s"' % self.defaults.identity)

        return uploadflags

    def get_python(self):
        """Get the Python interpreter.
        """
        self.python.check_valid_python()

    def get_options(self):
        """Process the command line.
        """
        args = self.parse_options(self.args)

        if args:
            self.directory = args[0]

        if self.list:
            self.list_locations()

        if not self.locations:
            self.locations.extend(self.locations.get_default_location())

        if not self.skipupload:
            self.locations.check_valid_locations()

        if len(args) > 1:
            if self.urlparser.is_url(self.directory):
                self.branch = args[1]
            elif self.urlparser.is_ssh_url(self.directory):
                self.branch = args[1]
            else:
                err_exit('mkrelease: too many arguments\n%s' % USAGE)

        if len(args) > 2:
            err_exit('mkrelease: too many arguments\n%s' % USAGE)

    def get_package(self):
        """Get the URL or sandbox to release.
        """
        directory = self.directory
        scmtype = self.scmtype
        develop = not self.infoflags

        self.scm = self.scms.get_scm(scmtype, directory)

        if self.scm.is_valid_url(directory):
            directory = self.urlparser.abspath(directory)

            self.remoteurl = directory
            self.isremote = self.push = True
        else:
            directory = abspath(expanduser(directory))
            self.isremote = False

            self.scm.check_valid_sandbox(directory)
            self.setuptools.check_valid_package(directory)

            name, version = self.setuptools.get_package_info(directory, develop)
            print 'Releasing', name, version

            if not self.skipcommit:
                if self.scm.is_dirty_sandbox(directory):
                    self.scm.commit_sandbox(directory, name, version, self.push)

    def make_release(self):
        """Build and distribute the egg.
        """
        directory = self.directory
        infoflags = self.infoflags
        distcmd = self.distcmd
        distflags = self.distflags
        branch = self.branch
        scmtype = self.scm.name
        develop = not self.infoflags

        tempdir = abspath(tempfile.mkdtemp(prefix='mkrelease-'))
        try:
            if self.isremote:
                directory = join(tempdir, 'build')
                self.scm.clone_url(self.remoteurl, directory)
            else:
                directory = abspath(expanduser(directory))

            self.scm.check_valid_sandbox(directory)

            if self.isremote:
                branch = self.scm.make_branchid(directory, branch)
                if branch:
                    self.scm.switch_branch(directory, branch)
                if scmtype != 'svn':
                    branch = self.scm.get_branch_from_sandbox(directory)
                    print 'Releasing branch', branch

            self.setuptools.check_valid_package(directory)

            if not (self.skipcommit and self.skiptag):
                self.scm.check_dirty_sandbox(directory)
                self.scm.check_unclean_sandbox(directory)

            name, version = self.setuptools.get_package_info(directory, develop)
            if self.isremote:
                print 'Releasing', name, version

            if not self.skiptag:
                print 'Tagging', name, version
                tagid = self.scm.make_tagid(directory, name, version)
                self.scm.check_tag_exists(directory, tagid)
                self.scm.create_tag(directory, tagid, name, version, self.push)

            if scmtype == 'svn' and self.scm.version_info[:2] < (1, 7):
                scmtype = 'svn_cvs'

            manifest = self.setuptools.run_egg_info(
                directory, infoflags, scmtype, self.quiet)
            distfile = self.setuptools.run_dist(
                directory, infoflags, distcmd, distflags, scmtype, self.quiet)

            if not self.skipupload:
                for location in self.locations:
                    if self.locations.is_server(location):
                        uploadflags = self.get_uploadflags(location)
                        if '--sign' in uploadflags and isfile(distfile+'.asc'):
                            os.remove(distfile+'.asc')
                        self.setuptools.run_register(
                            directory, infoflags, location, scmtype, self.quiet)
                        self.setuptools.run_upload(
                            directory, infoflags, distcmd, distflags, location, uploadflags,
                            scmtype, self.quiet)
                    elif self.locations.is_dist_url(location):
                        scheme = self.urlparser.get_scheme(location)
                        location = self.urlparser.to_ssh_url(location)
                        if scheme == 'sftp':
                            self.scp.run_sftp(distfile, location)
                        else:
                            self.scp.run_scp(distfile, location)
                    else:
                        self.scp.run_scp(distfile, location)
        finally:
            shutil.rmtree(tempdir)

    def run(self):
        self.get_python()
        self.get_options()
        self.get_package()
        self.make_release()
        print 'done'


def main(args=None):
    if args is None:
        args = sys.argv[1:]
    try:
        ReleaseMaker(args).run()
    except SystemExit, e:
        return e.code
    return 0


if __name__ == '__main__':
    sys.exit(main())

