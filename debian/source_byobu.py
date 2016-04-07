'''apport package hook for byobu

(c) 2009 Canonical Ltd.
Author: Dustin Kirkland <kirkland@byobu.org>
'''

from apport.hookutils import *
from os import path

def add_info(report):
    attach_related_packages(report, ['byobu*', 'screen*'])
    report['Binaries'] = command_output(['sh', '-c', 'ls -alF /usr/bin/screen* /usr/bin/byobu*'])
    attach_file_if_exists(report, path.expanduser('~/.screenrc'), 'ScreenRC')
