Byobu is a GPLv3 open source text-based window manager and terminal multiplexer. It was originally designed to provide elegant enhancements to the otherwise functional, plain, practical GNU Screen, for the Ubuntu server distribution. Byobu now includes an enhanced profiles, convenient keybindings, configuration utilities, and toggle-able system status notifications for both the GNU Screen window manager and the more modern Tmux terminal multiplexer, and works on most Linux, BSD, and Mac distributions.

For more information about this package, please visit:
  http://byobu.org

If *Byobu* is not packaged for your Linux or UNIX OS, or if you do not have administrative privileges in order to install Byobu, you may be able to install locally, using the following instructions...

### INSTALATION
 1. If you pull the source from the upstream bzr or git:

     ` bzr branch lp:byobu && cd byobu`
     `git clone git://github.com/dustinkirkland/byobu.git byobu-src`
	`cd byobu-src ./debian/rules autoconf`

 2. Or download the latest officially released version from:
      https://launchpad.net/byobu/+download

 3.  Extract:

     `tar zxvf byobu*.tar.gz && cd byobu*`

 4. Configure:

      `./configure --prefix="$HOME/byobu"`

       ***OPTIONAL*** : You may use python from your environment, rather than from your distro

       ***echo "export BYOBU_PYTHON='/usr/bin/env python'" >> $HOME/.bashrc***

 5. Build:
       `make`

 6. Install:

      `make install`

 7. Update your `PATH` and `BYOBU_PREFIX` environment variables

      `echo "export PATH=$HOME/byobu/bin:$PATH" >> $HOME/.bashrc`
      `. $HOME/.bashrc`

 8. Run:

      `byobu`

> Note that you will need to have a few dependencies installed:
 * tmux >= 1.5 and screen
 * python-newt (if you want to use Byobu's configuration utility)
 * gsed (if your sed implementation doesn't support -i)

### CONTRIBUTION

You may contribute to Byobu by branching the source from Launchpad (ideally), or by forking the project on Github (less ideally):

$ bzr branch lp:byobu

$ git clone git://github.com/dustinkirkland/byobu.git

You commit changes locally, and then propose a merge in Launchpad (ideally), or submit a pull request on Github (less ideally).

As for coding standards, please use tabs, rather than spaces.  Thanks!

### AUTHORS
Dustin Kirkland <kirkland@byobu.org>
Nick Barcet <nick.barcet@ubuntu.com>
Raphaël Pinson <raphink@ubuntu.com>
Derek Carter <goozbach@friocorte.com>

### LICENSE:
https://github.com/dustinkirkland/byobu/blob/master/COPYING

Dustin Kirkland <kirkland@byobu.org>

2019-11-29
