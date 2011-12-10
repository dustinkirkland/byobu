bin_SCRIPTS = byobu  byobu-config  byobu-ctrl-a  byobu-disable  byobu-enable  byobu-export  byobu-janitor  byobu-launch  byobu-launcher  byobu-launcher-install  byobu-launcher-uninstall  byobu-quiet  byobu-reconnect-sockets  byobu-select-backend  byobu-select-profile  byobu-select-session  byobu-silent  byobu-status  byobu-status-detail  byobu-shell

install-exec-hook:
	cd $(DESTDIR)$(bindir) && rm -f byobu-screen && $(LN_S) byobu byobu-screen
	cd $(DESTDIR)$(bindir) && rm -f byobu-screen && $(LN_S) byobu byobu-tmux
