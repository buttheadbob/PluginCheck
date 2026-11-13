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

## Get ready before a full run

A `behaviour` run can take hundreds of servers, so make sure it will work before starting one.

1. Finish the target first. Include every section the plugin has, and read its manifests to check that names and paths match.
2. A plugin that adds settings to Quasar needs `quasar_managed`, so check that it will run before the first full run. If a stage shows
   `not present`, find out why instead of calling it a gap.
3. Run the cheap stages first: `python check.py <target> --only manifest,policy,reflection,lint,magnetar,regcheck,torch`. Fix what they find, then
   run everything.
4. `behaviour` stops by itself as `not run` if a working build can't be discovered, because the rest of the run couldn't compare it. Fix the reason
   it gives, then run it again. A published build left out doesn't stop the run, it only narrows the comparison.
5. Changing what shapes a server throws away every saved server result: the server code in `behaviour.py` (`stages/behaviour/serverhash.py`
   says which), the fixture or the probes. Make those changes before a run, not between runs,
   and only when the user agrees.
6. Changing a plugin's source also needs a new command classification. Otherwise every command gets its own server and every setting is flipped.
7. While another run's `behaviour` has the machine, the first half of a run can go ahead in the pre-container lane:
   `python check.py <target> --pre`, then `python check.py <target> --continue <run folder>` once the machine is free (README,
   Pre-container lane). Both halves go in a terminal the user can see, like any run.

## Classify commands before a run

What a command touches is a judgement about its code, so it is yours to make. `behaviour` uses it to decide what shares a server.

1. Run `python check.py <target> --only commands`. It prints a `CLASSIFY` line for each command when no current reading exists.
2. Read every handler, and everything it calls.
3. Write the file it names, as `stages/classification.py` describes. List what each command `reads` and `writes`, the on/off `settings` its code can
   reach, and the file and line that show it in `why`. Use the same word for the same thing across commands. Only listed settings get flipped, so
   follow the code all the way down, and leave `settings` out if you cannot tell.
4. Give every command that takes a value real `examples`, using names that exist in the fixture, and a `caller` where the code acts on who ran it.
   A permission, privacy or "in-game only" check needs a `player` line and an `admin` line, or it has not been tested. A `NO EXAMPLE` line in
   `behaviour` is a gap in your reading, unless the command needs a list setting the fixture does not have.
5. Write example lines in the command's real case, as `commands` prints it. Torch does not find `!pblimiter getreport` for `!PBLimiter GetReport`.
6. A plugin may act only on things some targets have, such as programmable blocks. Work out what its code needs in the world before a run, and when a test world is what leaves code unreached, report what closes the gap: what must exist, how many, owned by whom, what any script must do, which code each item reaches, and who can build it. If it can be built, it is built before the full run.

Changing the world is not a reason to run alone. Commands share a server unless they overlap, so a command that pays a player and one that sets the
weather can share. Use `"writes": ["*"]` only for a command that could reach anything, such as one that restarts the server.

A command that changes something it does not declare fails loudly, and reruns alone. Source changes need a fresh reading.

## Check that commands really ran

Results say a server finished, and two builds that fail the same way agree. Neither says a command did its work. Before you report any `behaviour` result:

1. Read `behaviour-replies.txt` from start to finish, for every host. A reply such as "not found", "usage" or "in-game only" means the command stopped at
   its first check, so it was not tested, whatever the stage's status says.
2. Say in your report how many commands reached their logic on each host, and name the ones that did not. Never report a count of servers or
   results as if it were coverage.
3. Treat `FAILED ... never ran` as a defect in PluginCheck or the example. Find the cause before you give it a verdict. Never explain it away.
4. Claim only what ran. A host that only loaded the plugin proved that it loads, and a line that was never run is read code, not tested code.
5. Treat `TORCH-ARGS` as a command that did not get what was typed. Torch finds a command's arguments by searching the whole line for the first one, so
   `on` in `!monitor everything on` is found in "monitor". Read that reply, and give it a real example if `commands` warned about a value no example used.

## Read before you report

1. **Read the code behind every lead before you report it.** That is every `WARN`, `CHECK`, `LEAD`, `THREAD-SENSITIVE`, `DIFF`, `UNSTABLE`,
   `FAILED`, `noChange` entry and `logcheck` line, however many there are. Read the command handler, the methods it calls, and the code that
   decides who it applies to.
2. **Never report a guess.** Do not write "probably", "likely", "looks like", "seems", "should be", "I think" or "I have not read" about
   code you can read. Read it, then say what it does.
3. **Never pass on a label or description as evidence.** A command's description, a method's name or a stage's message is a claim. Read
   what the code does.
4. **Read the game's code for game behaviour.** Use the skill that matches where the plugin runs, from the table below. If the code does not
   settle it, measure it in a container.
5. **Never hand the user a list of leads to sort out.** The user gets your conclusions.

## Use the se-dev skills

No stage loads a skill, so using them is your job. Load the ones that fit the plugin before you read its code, and list the ones you used in your
report. Server and client code differ, so do not answer a server question from client code.

Being installed is not the same as being usable. Before you rely on a skill:

1. If its folder has a `Prepare.bat`, it needs setup once, and `Prepare.DONE` shows it is done. From PowerShell, run `.\Prepare.bat` in the skill's
   folder, in the background. Setup can take several minutes. For `se-dev-server-code`, set `SE_SERVER_ROOT` first if it cannot find the server.
2. Setup commits to the skill's own local repository, so it can fail with `gpg failed to sign the data` while signing is still starting. That is a
   timing problem. Run it again, and do not change any git setting.
3. `se-dev-plugin-review`'s preparation and posting scripts need Python 3.11+, `git` and a signed-in `gh`. If one is missing, report that as the
   blocker. Its `ReviewPolicy.md` and `ReviewPaths.md` need none of these, so read them anyway.

A skill you could not install or set up is `not run`, so say which one and why.

| Work | Skills |
|---|---|
| Every review, even with no hub PR yet | `se-dev-plugin-review`: read its `ReviewPolicy.md` and `ReviewPaths.md`, which hold the rules hub reviewers apply |
| A hub PR | `se-dev-plugin-review`, the whole skill. Its scripts need the PR's URL |
| Game behaviour on a server | `se-dev-server-code`, and `se-dev-server-book` first if it is installed |
| Game behaviour on a client | `se-dev-game-code`, and `se-dev-game-book` first if it is installed |
| Magnetar plugins | `se-dev-plugin` and `se-dev-plugin-sdk` |
| Torch plugins | `se-dev-torch`, and `se-dev-torch-book` if it is installed |
| Quasar UI plugins | `se-dev-quasar-plugin` |

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
- You never run git yourself. PluginCheck can use git to download repositories into its cache, and a skill's own setup and scripts can use the git
  and `gh` they need. Anything that changes the user's repositories or posts for them still needs the user to ask, each time.
- PluginCheck is fully automated. Never make a stage depend on a person writing or checking a per-plugin file.
- Never cap a run. There is always enough time for PluginCheck to run properly. The one exception:
  `behaviour` ends a single server that has written no log line for 10 minutes, has no record and is not compiling, and reports it as
  `FAILED … timeout` with its last lines. Retries stay as they are, and the run itself is never ended.
- A reused result is still a result. Judge it like any other, and say in your report what was reused.
- Comparisons are not tied to a platform. Never require both sides to be on the same one.
