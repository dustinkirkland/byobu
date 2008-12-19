#! /usr/bin/env python

import sys, os, os.path, time, string, dialog, commands

def ioctl_GWINSZ(fd):                  #### TABULATION FUNCTIONS
    try:                                ### Discover terminal width
        import fcntl, termios, struct, os
        cr = struct.unpack('hh', fcntl.ioctl(fd, termios.TIOCGWINSZ, '1234'))
    except:
        return None
    return cr

def terminal_size():                    ### decide on *some* terminal size
    cr = ioctl_GWINSZ(0) or ioctl_GWINSZ(1) or ioctl_GWINSZ(2)  # try open fds
    if not cr:                                                  # ...then ctty
        try:
            fd = os.open(os.ctermid(), os.O_RDONLY)
            cr = ioctl_GWINSZ(fd)
            os.close(fd)
        except:
            pass
    if not cr:                            # env vars or finally defaults
        try:
            cr = (env['LINES'], env['COLUMNS'])
        except:
            cr = (25, 80)
    return int(cr[1]-5), int(cr[0]-5)         # reverse rows, cols


def handle_exit_code(d, code):
    # d is supposed to be a Dialog instance
    if code in (d.DIALOG_CANCEL, d.DIALOG_ESC):
        if code == d.DIALOG_CANCEL:
            msg = "You chose cancel.  Do you want to " \
                  "exit this program?"
        else:
            msg = "You pressed ESC.  Do you want to " \
                  "exit this program?"
        # "No" or "ESC" will bring the user back to the demo.
        # DIALOG_ERROR is propagated as an exception and caught in main().
        # So we only need to handle OK here.
        if d.yesno(msg) == d.DIALOG_OK:
            sys.exit(0)
        return 0
    else:
        return 1                        # code is d.DIALOG_OK

def menu_demo(d, size):
    while 1:
        (code, tag) = d.menu(
            "Please chose an action",
            width=size[0],
            choices=[("1", "Display some basic help"),
                     ("2", "Change screen profile"),
                     ("3", "Create a new window"),
                     ("4", "Install screen by default at login")
                     ])
        if handle_exit_code(d, code):
            break
    return tag

def help(d, size):
    d.textbox("/usr/share/doc/screen-profiles/help.txt", width=size[0], height=size[1])

def profile(d):
    list = []
    for choice in commands.getoutput('select-screen-profile -l').splitlines():
        if choice.startswith("ubuntu"):
            el = (choice, "<-- recommended", 1)
            list.append(el)
        else:
            el = (choice, "", 0)
            list.append(el)
    (code, tag) = d.radiolist("Which profile would you like to use?", width=65, choices=list)
    if code == d.DIALOG_OK:
        commands.getoutput('select-screen-profile --set %s' % tag)
        d.msgbox("Please press F5 to apply profile")

def newwindow(d):
    (code, answer) = d.inputbox("New window name?", init="bash")
    if code == d.DIALOG_OK:
        commands.getoutput('screen -t %s' % answer)

def default(d):
    d.msgbox("This has yet to be implemented")

def main():
    """This is the main loop of our screen helper.

    """
    size = terminal_size()
    
    try:
        d = dialog.Dialog(dialog="dialog")
        d.add_persistent_args(["--backtitle", "GNU Screen profiles helper"])

        help(d, size)

        while True:
            tag = menu_demo(d, size)
            if   tag == "1":
                help (d, size)
            elif tag == "2":
                profile(d)
            elif tag == "3":
                newwindow(d)
            elif tag == "4":
                default(d)
            
    except dialog.error, exc_instance:
        sys.stderr.write("Error:\n\n%s\n" % exc_instance.complete_message())
        sys.stderr.write("%s\n" % dialog.error)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__": main()
