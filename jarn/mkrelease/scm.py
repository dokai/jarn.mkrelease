from os.path import join, isdir
from process import WithProcess
from dirstack import WithDirStack, chdir
from exit import err_exit


class SCM(WithProcess):
    """Interface to source code management systems."""

    name = ''

    def is_distributed(self):
        return False

    def is_valid_url(self, url):
        raise NotImplementedError

    def is_valid_sandbox(self, dir):
        return isdir(join(dir, '.%s' % self.name))

    def check_valid_sandbox(self, dir):
        if not self.is_valid_sandbox(dir):
            name = self.__class__.__name__
            err_exit('Not a %(name)s sandbox: %(dir)s' % locals())

    def is_remote_sandbox(self, dir):
        raise NotImplementedError

    def is_dirty_sandbox(self, dir):
        raise NotImplementedError

    def check_dirty_sandbox(self, dir):
        if self.is_dirty_sandbox(dir):
            err_exit('Uncommitted changes in %(dir)s' % locals())

    def is_unclean_sandbox(self, dir):
        raise NotImplementedError

    def check_unclean_sandbox(self, dir):
        if self.is_unclean_sandbox(dir):
            err_exit('Unclean sandbox: %(dir)s' % locals())

    def get_url_from_sandbox(self, dir):
        raise NotImplementedError

    def update_sandbox(self, dir):
        raise NotImplementedError

    def checkin_sandbox(self, dir, name, version, push):
        raise NotImplementedError

    def checkout_url(self, url, dir):
        raise NotImplementedError

    def get_tag_id(self, dir, version):
        raise NotImplementedError

    def tag_exists(self, dir, tagid):
        raise NotImplementedError

    def check_tag_exists(self, dir, tagid):
        if self.tag_exists(dir, tagid):
            err_exit('Tag exists: %(tagid)s' % locals())

    def create_tag(self, dir, tagid, name, version, push):
        raise NotImplementedError


class DSCM(SCM, WithDirStack):
    """Interface to distributed source code management systems."""

    name = ''

    def __init__(self, process=None):
        SCM.__init__(self, process)
        WithDirStack.__init__(self)

    def is_distributed(self):
        return True

    def is_remote_sandbox(self, dir):
        if not self.is_valid_sandbox(dir):
            return False
        return bool(self.get_url_from_sandbox(dir)) # XXX This may exit

    def get_tag_id(self, dir, version):
        return version


class Subversion(SCM):

    name = 'svn'

    def is_valid_url(self, url):
        return (url.startswith('svn://') or
                url.startswith('svn+ssh://') or
                url.startswith('http://') or
                url.startswith('https://') or
                url.startswith('file://'))

    def is_remote_sandbox(self, dir):
        return self.is_valid_sandbox(dir)

    def is_dirty_sandbox(self, dir):
        rc, lines = self.process.popen(
            'svn status "%(dir)s"' % locals(), echo=False)
        if rc == 0:
            lines = [x for x in lines if x[0:1] in ('M', 'A', 'R', 'D')]
            return bool(lines)
        return False

    def is_unclean_sandbox(self, dir):
        rc, lines = self.process.popen(
            'svn status "%(dir)s"' % locals(), echo=False)
        if rc == 0:
            lines = [x for x in lines if x[0:1] in ('M', 'A', 'R', 'D', 'C', '!', '~')]
            return bool(lines)
        return False

    def get_url_from_sandbox(self, dir):
        rc, lines = self.process.popen(
            'svn info "%(dir)s"' % locals(), echo=False)
        if rc == 0 and lines:
            return lines[1][5:]
        err_exit('Failed to get URL from %(dir)s' % locals())

    def update_sandbox(self, dir):
        rc = self.process.system(
            'svn update "%(dir)s"' % locals())
        if rc != 0:
            err_exit('Update failed')
        return rc

    def checkin_sandbox(self, dir, name, version, push):
        rc = self.process.system(
            'svn commit -m"Prepare %(name)s %(version)s." "%(dir)s"' % locals())
        if rc != 0:
            err_exit('Commit failed')
        return rc

    def checkout_url(self, url, dir):
        rc = self.process.system(
            'svn checkout "%(url)s" "%(dir)s"' % locals())
        if rc != 0:
            err_exit('Checkout failed')
        return rc

    def get_tag_id(self, dir, version):
        url = self.get_url_from_sandbox(dir)
        parts = url.split('/')
        if parts[-1] == 'trunk':
            parts = parts[:-1]
        elif parts[-2] in ('branches', 'tags'):
            parts = parts[:-2]
        else:
            err_exit('URL must point to trunk, branch, or tag: %(url)s' % locals())
        return '/'.join(parts + ['tags', version])

    def tag_exists(self, dir, tagid):
        rc, lines = self.process.popen(
            'svn list "%(tagid)s"' % locals(), echo=False, echo2=False)
        return rc == 0

    def create_tag(self, dir, tagid, name, version, push):
        url = self.get_url_from_sandbox(dir)
        rc = self.process.system(
            'svn copy -m"Tagged %(name)s %(version)s." "%(url)s" "%(tagid)s"' % locals())
        if rc != 0:
            err_exit('Tag failed')
        return rc


class Mercurial(DSCM):

    name = 'hg'

    def is_valid_url(self, url):
        return (url.startswith('ssh://') or
                url.startswith('http://') or
                url.startswith('https://') or
                url.startswith('file://'))

    @chdir
    def is_dirty_sandbox(self, dir):
        rc, lines = self.process.popen(
            'hg status -mar' % locals(), echo=False)
        if rc == 0:
            return bool(lines)
        return False

    @chdir
    def is_unclean_sandbox(self, dir):
        rc, lines = self.process.popen(
            'hg status -mard' % locals(), echo=False)
        if rc == 0:
            return bool(lines)
        return False

    @chdir
    def get_url_from_sandbox(self, dir):
        url = ''
        rc, lines = self.process.popen(
            'hg show paths.default', echo=False)
        if rc == 0:
            if lines:
                url = lines[0]
            else:
                return ''
        if not url:
            err_exit('Failed to get URL from %(dir)s' % locals())
        return url

    @chdir
    def update_sandbox(self, dir):
        if self.is_remote_sandbox(dir):
            rc = self.process.system(
                'hg pull -u')
            if rc != 0:
                err_exit('Update failed')
        return 0

    @chdir
    def checkin_sandbox(self, dir, name, version, push):
        rc = self.process.system(
            'hg commit -v -m"Prepare %(name)s %(version)s."' % locals())
        if rc != 0:
            err_exit('Commit failed')
        if push and self.is_remote_sandbox(dir):
            rc = self.process.system(
                'hg push')
            if rc != 0:
                err_exit('Push failed')
        return rc

    def checkout_url(self, url, dir):
        rc = self.process.system(
            'hg clone -v "%(url)s" "%(dir)s"' % locals())
        if rc != 0:
            err_exit('Checkout failed')
        return rc

    @chdir
    def tag_exists(self, dir, tagid):
        rc, lines = self.process.popen(
            'hg tags', echo=False)
        if rc == 0 and lines:
            for line in lines:
                if line.split()[0] == tagid:
                    break
            else:
                rc = 1
        else:
            rc = 1
        return rc == 0

    @chdir
    def create_tag(self, dir, tagid, name, version, push):
        rc = self.process.system(
            'hg tag -m"Tagged %(name)s %(version)s." "%(tagid)s"' % locals())
        if rc != 0:
            err_exit('Tag failed')
        if push and self.is_remote_sandbox(dir):
            rc = self.process.system(
                'hg push')
            if rc != 0:
                err_exit('Push failed')
        return rc


class Git(DSCM):

    name = 'git'

    def is_valid_url(self, url):
        return (url.startswith('git://') or
                url.startswith('ssh://') or
                url.startswith('rsync://') or
                url.startswith('http://') or
                url.startswith('https://') or
                url.startswith('file://'))

    @chdir
    def is_dirty_sandbox(self, dir):
        rc, lines = self.process.popen(
            'git status -a' % locals(), echo=False)
        return rc == 0

    @chdir
    def is_unclean_sandbox(self, dir):
        rc, lines = self.process.popen(
            'git status -a' % locals(), echo=False) # FIXME
        return rc == 0

    @chdir
    def get_url_from_sandbox(self, dir):
        url = ''
        rc, lines = self.process.popen(
            'git config -l', echo=False)
        if rc == 0 and lines:
            for line in lines:
                key_value = line.split('=', 1)
                if len(key_value) == 2:
                    key, value = key_value
                    if key == 'remote.origin.url':
                        url = value
                        break
            if not url:
                return ''
        if not url:
            err_exit('Failed to get URL from %(dir)s' % locals())
        return url

    @chdir
    def update_sandbox(self, dir):
        if self.is_remote_sandbox(dir):
            rc = self.process.system(
                'git pull')
            if rc != 0:
                err_exit('Update failed')
        return 0

    @chdir
    def checkin_sandbox(self, dir, name, version, push):
        rc = self.process.system(
            'git commit -a -m"Prepare %(name)s %(version)s."' % locals())
        if rc not in (0, 1):
            err_exit('Commit failed')
        if push and self.is_remote_sandbox(dir):
            rc = self.process.system(
                'git push --all origin')
            if rc != 0:
                err_exit('Push failed')
        return rc

    def checkout_url(self, url, dir):
        rc = self.process.system(
            'git clone "%(url)s" "%(dir)s"' % locals())
        if rc != 0:
            err_exit('Checkout failed')
        return rc

    @chdir
    def tag_exists(self, dir, tagid):
        rc, lines = self.process.popen(
            'git tag', echo=False)
        if rc == 0 and lines:
            for line in lines:
                if line == tagid:
                    break
            else:
                rc = 1
        else:
            rc = 1
        return rc == 0

    @chdir
    def create_tag(self, dir, tagid, name, version, push):
        rc = self.process.system(
            'git tag -m"Tagged %(name)s %(version)s." "%(tagid)s"' % locals())
        if rc != 0:
            err_exit('Tag failed')
        if push and self.is_remote_sandbox(dir):
            rc = self.process.system(
                'git push origin tag "%(tagid)s"' % locals())
            if rc != 0:
                err_exit('Push failed')
        return rc


class SCMContainer(object):
    """Hands out SCM objects."""

    scms = (Subversion, Mercurial, Git)

    def is_valid_url(self, url):
        for scm in self.scms:
            if scm().is_valid_url(url):
                return True
        return False

    def get_scm_from_type(self, type):
        for scm in self.scms:
            if scm.name == type:
                return scm()
        err_exit('Unknown SCM type: %(type)s' % locals())

    def get_scm_from_sandbox(self, dir):
        match = []
        for scm in self.scms:
            if scm().is_valid_sandbox(dir):
                match.append(scm)
        if not match:
            err_exit('Unknown sandbox: %(dir)s' % locals())
        if len(match) == 1:
            return match[0]()
        if len(match) == 2:
            types = '%s or %s' % tuple([x.name for x in match])
        elif len(match) == 3:
            types = '%s, %s, or %s' % tuple([x.name for x in match])
        err_exit('Failed to guess SCM type (may be %(types)s): %(dir)s' % locals())

    def get_scm_from_url(self, url):
        protocol = url.split('://', 1)[0]
        if protocol in ('svn', 'svn+ssh'):
            return Subversion()
        if protocol in ('git', 'rsync'):
            return Git()
        if protocol in ('ssh',):
            if url.endswith('.hg'):
                return Mercurial()
            if url.endswith('.git'):
                return Git()
            err_exit('Failed to guess SCM type (may be hg or git): %(url)s' % locals())
        if protocol in ('http', 'https', 'file'):
            if url.endswith('.hg'):
                return Mercurial()
            if url.endswith('.git'):
                return Git()
            err_exit('Failed to guess SCM type (may be svn, hg, or git): %(url)s' % locals())
        err_exit('Unknown URL: %(url)s' % locals())

    def guess_scm(self, type, url_or_dir):
        if type:
            return self.get_scm_from_type(type)
        if self.is_valid_url(url_or_dir):
            return self.get_scm_from_url(url_or_dir)
        return self.get_scm_from_sandbox(url_or_dir)

