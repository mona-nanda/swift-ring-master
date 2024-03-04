"""
Random utils
"""
from hashlib import md5
from os import mkdir
from swift.common.utils import drop_privileges
from swift.common.ring import Ring
from os.path import basename, join as pathjoin
from shutil import copy
from errno import EEXIST
import sys
import os
import atexit
import smtplib
import eventlet 
from signal import SIGTERM
from time import time, sleep
# logging doesn't import patched as cleanly as one would like
from logging.handlers import SysLogHandler, TimedRotatingFileHandler
import logging
logging.thread = eventlet.green.thread
logging.threading = eventlet.green.threading
logging._lock = logging.threading.RLock()
# setup notice level logging
NOTICE = 25
logging._levelToName[NOTICE] = 'NOTICE'


class EmailNotify(object):
    """Email (smtplib) based Notifications"""

    def __init__(self, conf, logger):
        self.conf = conf
        self.logger = logger
        self.smtp_host = conf.get('smtplib_host', 'localhost')
        self.smtp_port = int(conf.get('smtplib_port', '25'))
        self.from_addr = conf.get('smtplib_from_addr', 'ringmaster@localhost')
        self.recipients = [x.strip() for x in conf.get(
            'smtplib_recipients').split(',')]
        if not self.recipients:
            raise Exception('No smtplib recipients in conf.')

    def send_message(self, subject, body):
        """Send email with the provided subject and body"""
        message = """From: %s
        To: %s
        Subject: %s

        %s
        """ % (self.from_addr, self.recipients, subject, body)
        try:
            conn = smtplib.SMTP(self.smtp_host, self.smtp_port)
            conn.ehlo()
            conn.sendmail(self.from_addr, self.recipients, message)
            conn.close()
            return True
        except Exception:
            self.logger.exception('Email notification error.')
            return False


def get_file_logger(name, log_path, level=logging.INFO, count=7, fmt=None):
    logger = logging.getLogger(name)
    handler = TimedRotatingFileHandler(log_path, when='midnight',
                                       backupCount=count)
    formatter = logging.Formatter('%(asctime)s - %(name)s: %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


def get_md5sum(filename, chunk_size=4096):
    """Get the md5sum of a file

    :param filename: file to obtain the md5sum of
    :param chunk_size: chunk size
    :returns: hex digest of file
    """
    md5sum = md5()
    with open(filename, 'rb') as tfile:
        block = tfile.read(chunk_size)
        while block:
            md5sum.update(block)
            block = tfile.read(chunk_size)
    return md5sum.hexdigest()


def md5matches(target_file, expected_md5):
    """Check if a file matches an md5sum

    :param target_file: file to check
    :param expected_md5: md5 to compare again
    :returns: True or False if md5 matches
    """
    if get_md5sum(target_file) == expected_md5:
        return True
    else:
        return False


def make_backup(filename, backup_dir):
    """ Create a backup of a file
    :param filename: The file to backup
    :param backup_dir: The directory where to backup the file
    :returns: List of backed up filename and md5sum of backed up file
    """
    try:
        mkdir(backup_dir)
    except OSError as err:
        if err.errno != EEXIST:
            raise
    backup = pathjoin(backup_dir, '%d.' % time() + basename(filename))
    copy(filename, backup)
    return [backup, get_md5sum(backup)]


def is_valid_ring(ring_file):
    """Check if a ring file is 'valid'
        - make sure it has more than one device
        - make sure get_part_nodes works
    :returns: True or False if ring is valid
    """
    try:
        ring = Ring(ring_file)
        if len(ring.devs) < 1:
            return False
        if not ring.get_part_nodes(1):
            return False
    except Exception:
        return False
    return True


# http://www.jejik.com/articles/2007/02/a_simple_unix_linux_daemon_in_python/


class Daemon:
    """
    A generic daemon class.

    Usage: subclass the Daemon class and override the run() method
    """

    def __init__(self, pidfile, stdin='/dev/null', stdout='/dev/null',
                 stderr='/dev/null', user=None, group=None):
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.pidfile = pidfile
        self.uid = user
        self.gid = group

    def daemonize(self):
        """
        do the UNIX double-fork magic, see Stevens' "Advanced
        Programming in the UNIX Environment" for details (ISBN 0201563177)
        http://www.erlenstar.demon.co.uk/unix/faq_2.html#SEC16
        """
        try:
            pid = os.fork()
            if pid > 0:
                # exit first parent
                sys.exit(0)
        except OSError as err:
            sys.stderr.write("fork #1 failed: %d (%s)\n" %
                             (err.errno, err.strerror))
            sys.exit(1)

        # decouple from parent environment
        os.chdir("/")
        os.setsid()
        os.umask(0)

        # do second fork
        try:
            pid = os.fork()
            if pid > 0:
                # exit from second parent
                sys.exit(0)
        except OSError as err:
            sys.stderr.write("fork #2 failed: %d (%s)\n" %
                             (err.errno, err.strerror))
            sys.exit(1)

        # redirect standard file descriptors
        sys.stdout.flush()
        sys.stderr.flush()
        stin = file(self.stdin, 'r')
        stout = file(self.stdout, 'a+')
        sterr = file(self.stderr, 'a+', 0)
        os.dup2(stin.fileno(), sys.stdin.fileno())
        os.dup2(stout.fileno(), sys.stdout.fileno())
        os.dup2(sterr.fileno(), sys.stderr.fileno())
        # write pidfile
        atexit.register(self.delpid)
        pid = str(os.getpid())
        file(self.pidfile, 'w+').write("%s\n" % pid)
        drop_privileges(user=self.uid)

    def delpid(self):
        """Remove pid file"""
        os.remove(self.pidfile)

    def start(self, *args, **kw):
        """
        Start the daemon
        """
        # Check for a pidfile to see if the daemon already runs
        try:
            pidfile = file(self.pidfile, 'r')
            pid = int(pidfile.read().strip())
            pidfile.close()
        except IOError:
            pid = None

        # make sure we have access to write a pid file
        if not os.access(os.path.dirname(self.pidfile), os.W_OK):
            message = "No write access to create pid file %s\n"
            sys.stderr.write(message % self.pidfile)
            sys.exit(1)

        if pid:
            message = "pidfile %s already exist. Daemon already running?\n"
            sys.stderr.write(message % self.pidfile)
            sys.exit(1)

        # Start the daemon
        self.daemonize()
        self.run(*args, **kw)

    def stop(self):
        """
        Stop the daemon
        """
        # Get the pid from the pidfile
        try:
            pidfile = file(self.pidfile, 'r')
            pid = int(pidfile.read().strip())
            pidfile.close()
        except IOError:
            pid = None

        if not pid:
            message = "pidfile %s does not exist. Daemon not running?\n"
            sys.stderr.write(message % self.pidfile)
            return  # not an error in a restart

        try:
            while 1:
                os.kill(pid, SIGTERM)
                sleep(0.1)
        except OSError as err:
            err = str(err)
            if err.find("No such process") > 0:
                if os.path.exists(self.pidfile):
                    os.remove(self.pidfile)
            else:
                print(str(err))
                sys.exit(1)

    def restart(self, *args, **kw):
        """Restart the daemon"""
        self.stop()
        self.start(*args, **kw)
