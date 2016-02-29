bin_SCRIPTS = byobu  byobu-config  byobu-ctrl-a  byobu-disable  byobu-disable-prompt  byobu-enable  byobu-enable-prompt  byobu-export  byobu-janitor  byobu-keybindings  byobu-launch  byobu-launcher  byobu-launcher-install  byobu-launcher-uninstall  byobu-layout  byobu-prompt  byobu-quiet  byobu-reconnect-sockets  byobu-select-backend  byobu-select-profile  byobu-select-session  byobu-silent  byobu-status  byobu-status-detail  byobu-shell byobu-ugraph byobu-ulevel col1 ctail wifi-status vigpg manifest purge-old-kernels

install-exec-hook:
	cd $(DESTDIR)$(bindir) && rm -f byobu-screen && $(LN_S) byobu byobu-screen
	cd $(DESTDIR)$(bindir) && rm -f byobu-tmux   && $(LN_S) byobu byobu-tmux
