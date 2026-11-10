+++
series = "Planning with the simulator"
episode = "Video 2a"
title = "Windows: getting WSL ready"
subtitle = "A Linux terminal on Windows, for the simulator"
data = "prototype_data"

[youtube]
title = "WSL for Windows"
summary = """
For Windows only: Alice turns on WSL, the Linux built into Windows, and \
gets the terminal that video 2 starts from. Setup guide: \
https://github.com/BobKerns/analysis/blob/main/docs/SETUP.md#windows-set-up-wsl-first"""
contents = [
  "Why WSL",
  "Turning it on (wsl --install)",
  "Your Linux user name and password",
  "Opening Ubuntu again",
  "Your Linux files, from File Explorer and from Linux",
  "VS Code with the WSL extension, and charts in its terminal",
]
tags = ["WSL", "Windows"]
# A side trip for Windows viewers before video 2: it leads there, and
# video 2 goes on to video 3 without it.
previous = ""
next = "02-setup"
+++

A companion to video 2 (issue #389, part of #366), for Windows viewers only:
turning on WSL, making the Linux user, finding the terminal and the files
again, and VS Code. It ends where video 2's install scene begins, so Mac and
Linux viewers never sit through it.

The tapes record a Linux terminal, and nothing else, so the Windows screens
are drawn by the `window` slide template from the text in each slide block:
they re-render and translate like any other slide, with no Windows machine.
They show what the screens say, not a pixel copy of them. The last scene is
a real tape, since the Ubuntu terminal is a Linux terminal.

## Scene `title`: Title card

```slide
template = "title"
```

Planning with the simulator. Video two A: getting Windows ready.

This one is for Windows. The simulator runs on Linux and on a Mac. Windows
can run Linux too, built in, with a part of Windows called WSL. In a few
minutes, Alice turns it on, and gets the terminal that video two starts
from.

## Scene `why`: Why WSL

```slide
template = "card"
heading = "Why WSL"
body = [
  "WSL: the Windows Subsystem for Linux",
  "A real Linux, Ubuntu, inside Windows",
  "Free, from Microsoft, part of Windows 10 and 11",
]
speaker = "alice"
```

WSL stands for the Windows Subsystem for Linux. It runs a real Linux,
called Ubuntu, inside Windows, side by side with your Windows programs. It's
free, it comes from Microsoft, and it's part of Windows 11, and of Windows
10 from its 2020 updates on. The simulator, its installer and its tools all
run in that Linux, exactly as they would on a Linux computer. And your
Windows files and programs stay just as they are.

## Scene `turn-on`: Turning it on

```slide
template = "window"
heading = "Turning WSL on"
title = "Administrator: Windows PowerShell"
lines = [
  { prompt = "PS C:\\WINDOWS\\system32>", typed = "wsl --install" },
  "Installing: Virtual Machine Platform",
  "Virtual Machine Platform has been installed.",
  "Installing: Windows Subsystem for Linux",
  "Windows Subsystem for Linux has been installed.",
  "Installing: Ubuntu",
  "Ubuntu has been installed.",
  "The requested operation is successful. Changes will not be effective until the system is rebooted.",
]
note = "Right-click Start: Terminal (Admin). On Windows 10: PowerShell (Admin)"
```

Turning it on takes one command, run as an administrator. Right-click the
Start button, and pick Terminal, Admin. On Windows 10, it's Windows
PowerShell, Admin. Say yes when Windows asks whether to allow it. Type
{wsl --install|w s l, space, dash dash install}, and press Enter. It turns on what Linux needs, and
downloads Ubuntu. That takes a few minutes. When it says so, restart the
computer.

> Production note: `wsl --install`'s messages differ a little between
> Windows versions, and on a computer that already has the pieces it may
> start Ubuntu at once, without a restart. The narration says "when it says
> so" for that reason. If it fails with a message about virtualization, the
> setup guide's WSL section says what to do; the video doesn't.

## Scene `linux-user`: Your Linux user name

```slide
template = "window"
heading = "The first start: a Linux user"
title = "Ubuntu"
lines = [
  "Installing, this may take a few minutes...",
  "Please create a default UNIX user account. The username does not need to match your Windows username.",
  { prompt = "Enter new UNIX username:", typed = "alice" },
  { prompt = "New password:", secret = true },
]
```

After the restart, Ubuntu opens by itself, and finishes installing. Then it
asks for a user name, for Linux. It doesn't have to match your Windows name:
one word, in lowercase, is best. Alice types alice. Then it asks for a
password.

## Scene `password`: A password, typed blind

```slide
template = "window"
heading = "A password, typed blind"
title = "Ubuntu"
lines = [
  { prompt = "Enter new UNIX username:", typed = "alice" },
  { prompt = "New password:" },
  { prompt = "Retype new password:" },
  "passwd: password updated successfully",
  "Installation successful!",
  { prompt = "alice@home:~$", secret = true },
]
note = "Nothing appears as you type the password. That's normal."
```

As you type the password, nothing appears: no dots, no stars. That's normal.
Type it, press Enter, and type it again. Pick one you'll remember. Linux
asks for it before it installs software, and the simulator's installer will
ask for it once, to install the tools it needs. Then you see a prompt, with
your name, and a dollar sign. That's a Linux terminal, ready for commands.

## Scene `open-again`: Opening it again

```slide
template = "card"
heading = "Opening Ubuntu again"
body = [
  "Start menu: Ubuntu",
  "Windows Terminal: the arrow beside the tab, then Ubuntu",
  "To open Ubuntu every time: Settings, Startup, Default profile",
]
speaker = "bill"
```

Next time, open Ubuntu from the Start menu: type Ubuntu, and pick it. Or, in
Windows Terminal, which comes with Windows 11, click the little arrow
beside the tab, and pick Ubuntu. If you'll mostly use it for the simulator,
make Ubuntu what Windows Terminal opens every time: in its Settings, under
Startup, set the default profile to Ubuntu.

## Scene `files`: Where your files live

```slide
template = "window"
heading = "Your Linux files, from Windows"
title = "File Explorer"
look = "files"
address = "\\\\wsl$\\Ubuntu\\home\\alice\\retirement"
lines = ["analysis", "data"]
note = "Or click Linux, at the bottom of File Explorer's left side"
```

Linux keeps its own files, in your Linux home folder. That's where the
installer puts the simulator, and your data: in a folder called retirement,
with analysis, the code, and data, your household. Windows can reach them.
In File Explorer, click Linux, at the bottom of the left side. Or type
{\\wsl$|two backslashes, w s l, dollar} in the address bar. Then Ubuntu, home, and your
Linux name.

> Production note: `\\wsl.localhost\Ubuntu` is the newer spelling of the
> same place; `\\wsl$` works on every version.

## Scene `explorer`: And from Linux

```slide
template = "window"
heading = "From Linux: open the folder in Windows"
title = "Ubuntu"
lines = [
  { prompt = "alice@home:~$", typed = "cd ~/retirement" },
  { prompt = "alice@home:~/retirement$", typed = "explorer.exe ." },
]
note = "Keep the simulator and its data in Linux, not on C:"
```

From the Linux side, {explorer.exe .|explorer dot exe, space, dot} opens
the folder you're in, in File Explorer. Going the other way, your Windows C
drive is in Linux at {/mnt/c|slash m n t slash c}. But keep the simulator and its data
in your Linux home, where the installer puts them. It's much faster there,
and Linux's tools, like git, behave as they should.

## Scene `vscode`: VS Code

```slide
template = "card"
heading = "VS Code, working in Linux"
body = [
  "Install VS Code in Windows",
  "Add the WSL extension",
  "In Ubuntu: code ~/retirement/analysis",
]
speaker = "alice"
```

To use the notebooks, and a terminal that can show pictures, get VS Code:
Microsoft's free editor. Install it in Windows, as usual, not inside
Ubuntu. In VS Code's Extensions, add the one called WSL. Then, in Ubuntu,
type code, then the code folder: {~/retirement/analysis|retirement slash analysis}. VS
Code opens it, running its side of things in Linux. The first time, that
takes a minute.

## Scene `vscode-window`: Pictures in VS Code's terminal

```slide
template = "window"
title = "analysis [WSL: Ubuntu] - Visual Studio Code"
look = "editor"
status = "WSL: Ubuntu"
lines = [
  "Settings: Terminal › Integrated: Enable Images",
  "",
  { prompt = "alice@home:~/retirement/analysis$", typed = "sim chain show spending --graph" },
]
note = "Windows Terminal saves the chart as a picture file instead, and says where"
```

The corner of the window says WSL, Ubuntu: VS Code is working in Linux. Its
notebooks run there, and so does its terminal. To see charts right in that
terminal, open Settings, search for enable images, and turn on the one under
Terminal, Integrated. Windows Terminal can't draw them; the same commands
save the chart as a picture file there, and say where to find it.

## Scene `ready`: Ready for video two

```tape
Set FontSize 27
Sleep 500ms
Type "curl -fsSL https://raw.githubusercontent.com/BobKerns/analysis/main/scripts/install.sh | sh"
Sleep 12s
```

That's all Windows needs. From here on, everything happens in the Ubuntu
terminal, and works just as on a Mac or Linux computer. Video two starts
right here: open Ubuntu, and paste the install line from the setup guide.
In Windows Terminal, Control V pastes.

> Production note: as in video 2's install scene, the tape stops short of
> Enter.

## Scene `end`: Next

```slide
template = "card"
heading = "Next: video 2, setup and install"
body = "Paste the install line in Ubuntu, and video 2 takes it from there."
speaker = "alice"
```

Next: video two, setup and install. It's linked in the description.
