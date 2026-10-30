+++
series = "Planning with the simulator"
episode = "Video 2"
title = "Setup and install"
subtitle = "From nothing to a data folder of your own"
data = "prototype_data"
+++

The second video of #366 (issue #374): installing with the one-line
installer (#308), starting a data repo with `sim init` (#289), activating
it and the current scenario, the notebooks, and what `sim run` offers to
commit.

The tapes don't run the installer itself: it downloads the code and takes
minutes. The install scene types the line without pressing Enter, and the
narration says what it does. Every later step is recorded for real, in a
fresh home folder: `setup.sh` beside this script makes the data repo the
installer would have made, and the init scene makes it again on camera.

Notebooks are left to video 4, until the pipeline can record them; the
notebooks scene is a card.

## Scene `title`: Title card

```slide
template = "title"
```

Planning with the simulator. Video two: setup and install.

In this video, Alice gets the simulator running on her own computer, and
starts a folder for her household's data. By the end, she can run a plan,
and every run she makes can be repeated exactly.

## Scene `install`: One line to install

```tape
Set FontSize 27
Sleep 500ms
Type "curl -fsSL https://raw.githubusercontent.com/BobKerns/analysis/main/scripts/install.sh | sh"
Sleep 21s
```

Installing takes one line. Open a terminal, on a Mac, on Linux, or on
Windows under WSL, and paste this line from the setup guide. It downloads
the installer and runs it. You don't need to know git, or anything else
about how the code is kept. On Windows, set up WSL first: there's a short
video for that, linked in the description, along with the setup guide.

> Production note: the tape stops short of Enter. A real install downloads
> the code and takes several minutes, so the next scene says what happens.

## Scene `what-install-does`: What the installer does

```slide
template = "card"
heading = "What the installer does"
body = [
  "Gets git, if your computer doesn't have it",
  "Puts the code in retirement/analysis",
  "Sets up Python and everything the code needs",
  "Makes your data folder in retirement/data",
]
speaker = "alice"
```

The installer asks two questions first: where the code should go, and where
your data should go. The suggested places, a retirement folder in your home
folder, are fine for most people. Then it installs git if it's missing. On a
Mac that's Apple's command line tools: finish installing them in the window
that opens, and paste the line again. It downloads the code, and sets up
Python and everything the code needs. That part takes a few minutes the
first time. Last, it makes your data folder, asking who you are. To update
later, paste the same line again.

## Scene `terminals`: A terminal that shows pictures

```slide
template = "card"
heading = "A terminal that shows pictures"
body = [
  "Mac: iTerm2 or Ghostty",
  "Linux: kitty, Ghostty or WezTerm",
  "Anywhere: VS Code's terminal",
  "Any other terminal saves the picture as a file",
]
speaker = "bill"
```

Any terminal runs the simulator. A few commands can also draw a chart right
in the terminal, and for that you want one that shows pictures. On a Mac,
iTerm2 or Ghostty. On Linux, kitty, Ghostty or WezTerm. Or, anywhere, VS
Code's own terminal, with images turned on in its settings. In any other
terminal, the chart is saved as a picture file instead, and the command says
where. The setup guide keeps the list.

## Scene `two-folders`: Two folders, and why

```slide
template = "card"
heading = "Two folders: the code, and your data"
body = [
  "retirement/analysis: the simulator. It updates without touching your data.",
  "retirement/data: your household. Plain text files, with their history.",
]
```

So you end up with two folders. One holds the code: the simulator itself.
The other holds your data: your household, your accounts, your plans. They
are kept apart on purpose. The code can be updated without touching your
numbers, and your numbers never leave your computer. Each folder keeps its
own history, so every run can record exactly which version of your data,
and of the code, produced it.

## Scene `init`: Starting a data folder

```tape
Hide
Type "export HOME=$PWD/home && rm -rf ~/retirement && cd && clear"
Enter
Sleep 300ms
Show
Sleep 500ms
Type "sim init ~/retirement/data"
Sleep 400ms
Enter
Sleep 1500ms
Type "alice"
Sleep 600ms
Enter
Sleep 1s
Type "Alice Example"
Sleep 600ms
Enter
Sleep 1s
Type "1962-04-15"
Sleep 600ms
Enter
Sleep 1s
Type "f"
Sleep 600ms
Enter
Sleep 1s
Enter
Wait
Sleep 9s
```

The installer makes your data folder with a command called sim init, and you
can run it yourself, too: say, to start a second folder to experiment in. It
asks who you are. An id, one word, that the data uses for you. Your name.
Your birth date, which sets your age for things like Medicare and required
distributions. Your sex, which picks a life table if you want the simulator
to vary how long you live; you can leave it blank. And the household's tax
filing status: married filing jointly is the default. Then it builds the
folder from the templates that come with the code, checks that the starting
scenario, base, loads, and saves a first version of everything.

> Production note: the tapes' environment sets `ANALYSIS_NO_BUILDER`, so
> this `sim init` doesn't link the recording checkout or turn on background
> builds. The installer turns them on for a real install.

## Scene `inside`: What's inside

```tape
Hide
Type "export HOME=$PWD/home && cd ~/retirement/data/sim && clear"
Enter
Sleep 300ms
Show
Sleep 500ms
Type "ls"
Sleep 400ms
Enter
Wait
Sleep 3s
Type "ls scenarios/base"
Sleep 400ms
Enter
Wait
Sleep 12s
```

Inside are plain text files, mostly spreadsheets you could open in any
spreadsheet program. Persons holds the people in the household. Scenarios
holds your plans, starting with base. Base is where your household goes:
its accounts, income, spending, and the rules that move money between them.
For now it's nearly empty; the next video fills it in. The tax tables and
other standard assumptions aren't copied here. They're {read|red} from the code,
so they stay current when the code updates.

## Scene `activate`: Activating your data

```tape
Hide
Type "export HOME=$PWD/home && cd && unset ANALYSIS_DATA_DIR && clear"
Enter
Sleep 300ms
Show
Sleep 500ms
Type "source ~/retirement/data/sim/activate-analysis"
Sleep 400ms
Enter
Wait
Sleep 9s
Type "sim switch"
Sleep 400ms
Enter
Wait
Sleep 8s
```

Each time you open a new terminal window, you activate your data with this
one line. It tells the sim command where your data is, and changes the
prompt to show it: your data, you, and the current scenario, base.
Most commands work on the current scenario unless you name another one.
Sim switch says which one is current.

> Production note: the prompt's first part is the data directory's own
> name, `sim`, rather than the data repo's (`data`); the narration doesn't
> name it. The `(analysis)` after it is the code's Python environment.

## Scene `plan`: A plan of your own

```tape
Set FontSize 24
Hide
Type "export HOME=$PWD/home && cd && unset ANALYSIS_DATA_DIR"
Enter
Type "source ~/retirement/data/sim/activate-analysis > /dev/null && clear"
Enter
Sleep 2s
Show
Sleep 500ms
Type "sim scenario create my_plan base --description 'Our first plan'"
Sleep 400ms
Enter
Wait
Sleep 8s
Type "sim switch base"
Sleep 400ms
Enter
Wait
Sleep 6s
```

A new scenario builds on another one. Sim scenario create makes my plan,
building on base, and makes it the current scenario: the prompt changes to
match. It starts with nothing of its own, so for now it's the same as base.
Sim switch, with a scenario's name, changes the current scenario back. So
does naming a scenario on any other command.

> Production note: "my plan" is spelled with a space so the voice doesn't
> say "underscore".

## Scene `notebooks`: The notebooks

```slide
template = "card"
heading = "The notebooks"
body = [
  "In an activated terminal: analysis jupyter",
  "Or open the code folder in VS Code",
  "Charts and tables, no code to write",
]
speaker = "bill"
```

Besides the terminal, the code comes with Jupyter notebooks: pages of charts
and tables that you explore with menus and sliders, without writing any
code. To open them, type analysis jupyter in an activated terminal. JupyterLab
opens in your web browser, with the notebooks on the left. Or, if you use VS
Code, open the code folder there, and open a notebook from its notebooks
folder. Video four goes through them.

## Scene `commit`: Saving your work

```tape
Set FontSize 26
Hide
Type "export HOME=$PWD/home && cd && unset ANALYSIS_DATA_DIR"
Enter
Type "source ~/retirement/data/sim/activate-analysis > /dev/null"
Enter
Type "sim scenario create my_plan base --description 'Our first plan' > /dev/null 2>&1 && clear"
Enter
Sleep 4s
Show
Sleep 500ms
Type "sim run"
Sleep 400ms
Enter
Sleep 12s
Enter
Wait
Sleep 10s
```

Every time you change your data, by a command like that one or by editing a
file, the change is new: nothing has recorded it yet. When you run a
scenario with changes like that, sim run lists them, and offers to save a
version first. Say yes, the default, and it saves them with a message saying
what changed, then runs. That's what makes a run repeatable: it records the
saved version it {read|red}, so you, or anyone you share it with, can get the same
answer later, and see what changed between two runs. With the installer's
background builds on, saving a change also reruns, in the background, every
scenario it affects. And sim commit saves your changes without running.

## Scene `end`: Next time

```slide
template = "card"
heading = "Next: your base scenario"
body = "Your household's accounts, income and spending, in the scenario everything else builds on."
speaker = "alice"
```

Next time: filling in your base scenario, with your household's accounts,
income and spending.
