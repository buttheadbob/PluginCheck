# Rules for the AI operating PluginCheck

You run PluginCheck and judge what it finds. A stage finds leads. You find out whether they are true. You can read a whole plugin and the
game's code faster than any developer, and that is the reason an AI runs this. These rules have no exceptions. [REVIEW.md](REVIEW.md)
says how to review, and these rules say how much of that work you must do.

## Run it in a terminal the user can see

`check.py` draws a live colour panel with the time left, every stage, each running server and the results so far. It must always be shown,
because it is how the user watches a run. `stages/display.py` only draws it on a real terminal, and silently falls back to plain text anywhere else.

1. Start every run in a real terminal the user can open, including a single stage or `--only`. In the Claude desktop app that is
   `run_in_terminal`, which opens a tab in the Terminal panel. Link the tab in your message.
2. Never hide it. Do not start a run from a Bash or PowerShell tool or as a background task, do not redirect or pipe its output with `>`, `|` or
   `tee`, and do not set `PLUGINCHECK_PLAIN=1` unless the user asks. Any of these turns the panel off.
3. Follow the run from its files, not its output. `runs/<target>/<timestamp>/results.json` is updated after every stage, and each stage's log
   can be followed live, so nothing needs capturing.

## Classify commands before a run

What a command touches is a judgement about its code, so it is yours to make. `behaviour` uses it to decide what shares a server.

1. Run `python check.py <target> --only commands`. It prints a `CLASSIFY` line for each command when no current reading exists.
2. Read every handler, and everything it calls.
3. Write the file it names, as `stages/classification.py` describes. List what each command `reads` and `writes`, the on/off `settings` its code can
   reach, and the file and line that show it in `why`. Use the same word for the same thing across commands. Only listed settings get flipped, so
   follow the code all the way down, and leave `settings` out if you cannot tell.

Changing the world is not a reason to run alone. Commands share a server unless they overlap, so a command that pays a player and one that sets the
weather can share. Use `"writes": ["*"]` only for a command that could reach anything, such as one that restarts the server.

A command that changes something it does not declare fails loudly, and reruns alone. Source changes need a fresh reading.

## Read before you report

1. **Read the code behind every lead before you report it.** That is every `WARN`, `CHECK`, `LEAD`, `THREAD-SENSITIVE`, `DIFF`, `UNSTABLE`,
   `FAILED`, `noChange` entry and `logcheck` line, however many there are. Read the command handler, the methods it calls, and the code that
   decides who it applies to.
2. **Never report a guess.** Do not write "probably", "likely", "looks like", "seems", "should be", "I think" or "I have not read" about
   code you can read. Read it, then say what it does.
3. **Never pass on a label or description as evidence.** A command's description, a method's name or a stage's message is a claim. Read
   what the code does.
4. **Read the game's code for game behaviour.** Use the decompiled game and the `se-dev-game-code` skill. If the code does not settle it,
   measure it in a container.
5. **Never hand the user a list of leads to sort out.** The user gets your conclusions.

## Every lead gets a verdict

Report each lead with one verdict, and the file and line that prove it:

| Verdict | Meaning |
|---|---|
| `real` | The problem exists. Say what a player or server can do, and how |
| `intended` | It does what the lead says, on purpose. Show why you can tell, such as a public leaderboard |
| `not a problem` | The lead is wrong. Say what in the code rules it out |
| `unable to verify` | You tried, and neither the code nor a test could settle it. Say what you tried |

`unable to verify` is never a substitute for reading. Code you have access to and did not read is not `unable to verify`. It is work you
have not finished.

## When a stage is wrong

A false alarm is a defect in PluginCheck. Do not only note it: when you are working on PluginCheck, fix the stage so it does not happen
again, and test the fix. When you are only reviewing a plugin, tell the user what the stage got wrong.

## Limits

- Never change a plugin during a review unless the user asks. Report it.
- Never run a plugin outside the containers.
- You never run git yourself. PluginCheck can use git to download repositories into its cache.
- PluginCheck is fully automated. Never make a stage depend on a person writing or checking a per-plugin file.
- Never cap a run. There is always enough time for PluginCheck to run properly.
- A reused result is still a result. Judge it like any other, and say in your report what was reused.
- Comparisons are not tied to a platform. Never require both sides to be on the same one.
