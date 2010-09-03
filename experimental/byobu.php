<?php

/*

    byobu.php: web application interface to running byobu sessions
    Copyright (C) 2010 Dustin Kirkland <kirkland@canonical.com>

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as
    published by the Free Software Foundation, version 3 of the
    License.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.

*/


/* Set up some global variables */
$USER = rtrim(`whoami`);
$EXCHANGE = "/var/run/screen/S-$USER/byobu-exchange";
$HOST = $_SERVER["HTTP_HOST"];
$ENV = "HOME=~$USER";
$TITLE = "$USER@$HOST - byobu web interface";

function cmd($command) {
/* Run a backtick command on the system, but first, prepend any
   environment variables that we need to (over)load. */
	global $ENV;
	return `$ENV $command`;
}

function markup($input) {
/* Translate screen's text color/attribute markup to HTML */
/* TODO: this function is just a stub right now */
	if (preg_match("/\{\+b.*\}/", $input)) {

	}
	$output = preg_replace("/\{[^\}]+\}/", "", $input);
	return($output);
}

function backticks() {
/* Run all backtick status commands, load and return a hash of statuses */
	$lines = preg_grep("/^backtick.*byobu-status/", file("/usr/share/byobu/profiles/common"));
	foreach ($lines as $line) {
		$key = preg_replace("/^backtick /", "", $line);
		$key = preg_replace("/[^0-9].*/", "", $key);
		$value = rtrim(preg_replace("/.*byobu-status /", "", $line));
		switch ($value) {
			case "date":
				$status = date("Y-m-d");
				break;
			case "time":
				$status = date("H:i:s");
				break;
			default:
				//$status = markup(cmd("byobu-status $value"));
				$status = markup(htmlspecialchars(cmd("/usr/lib/byobu/$value")));
				break;
		}
		$backticks["$key"] = "<td>" . $status . "</td>";
		//print("$key -> $value -> $status<br>");
	}
	return $backticks;
}

function print_status() {
/* Print the bottom two status lines of a byobu session */
/* TODO: alignment of items needs a little love */
	$backticks = backticks();
	$rows = array("^caption always ", "^hardstatus string ");
	print("<hr><pre>");
	foreach ($rows as $row) {
		print("<table cellpadding=1 cellspacing=1 width=100%><tr>");
		$line = array_pop(preg_grep("/$row/", file("/usr/share/byobu/profiles/common")));
		$items = preg_split("/[^0-9]+/", $line);
		for ($i=0; $i<sizeof($items); $i++) {
			print($backticks[$items[$i]]);
		}
		print("</tr></table>");
	}
	print("</table></pre>");
}

function process($command) {
	/* Process new command, if posted */
	$command = escapeshellcmd($_POST["q"]);
	/* TODO: Currently runs against screen "0"; should have a mechanism
                 web page for selecting a session */
	cmd("screen -X at 0 stuff \"$command\"");
	/* TODO: This sleep is gross, but some locking will be necessary to
                 ensure that (most) commands execute and complete before the
                 screen exchange happens */
	sleep(2);

}

function print_screen_contents($exchange) {
	/* TODO: Currently runs against screen "0"; should have a mechanism
                 web page for selecting a session */
	cmd("screen -X at 0 eval 'process s' 'exec sed -i \"/./,/^$/!d\" $exchange'");
	/* TODO: This sleep is gross, but it takes a little bit of time for
                 screen to write the window buffer to a file.  Need to speed
                 this up as much as possible. */
	sleep(2);
	print("<pre>" . file_get_contents($exchange) . "</pre>");
}

if (isset($_POST["q"])) {
	process($_POST["q"]);
}

?>

<html>
<head>
<title><?php print("$TITLE"); ?></title>
</head>
<script>
<!--
/* Force the page to scroll all the way to the bottom, and put focus in the
   text box, make it look/feel like a shell */
function bottom() {
    	window.scrollBy(0,100000);
	document.f.q.focus();
}
// -->
</script>
<body onLoad=bottom()>
<?php print_screen_contents($EXCHANGE); ?>
<form name=f method=post><input tabindex=1 type=text name=q size=80><input type=submit></form>
<?php print_status(); ?>
</body>
</html>
