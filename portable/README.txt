AmiAmi ordering measurement — portable
======================================

Copy this whole folder onto a memory stick and run it from a laptop. Nothing
is installed into the machine and nothing is left behind: the one dependency
lands in a "lib" folder beside these files, and the results land in
"measurement.json" next to them.


WHAT IT ANSWERS
---------------
The crawler wants to notice a newly listed second-hand copy quickly without
re-reading all 213 pages of the pre-owned catalogue every hour. That only
works if the ordering it reads carries new arrivals towards the front.

For the shop's "newest first" ordering it measurably does not: over 24.7 hours
only 9 of 592 arrivals ever showed up in the first twenty pages — worse than
picking pages at random. The shop's own 中古 ordering looked far better in a
one-off comparison. This is the proper check on that.


HOW TO RUN IT
-------------
1. Double-click start.bat
2. Leave the window open for 24 hours
3. The report prints itself at the end

The first run on a new machine takes a moment to set itself up and needs
internet for that. Every run needs internet anyway.

If Python is not on the laptop, start.bat says so. Install it from
python.org/downloads and tick "Add python.exe to PATH" on the installer's
first screen.


IMPORTANT: THE LAPTOP MUST STAY AWAKE
-------------------------------------
A sleeping laptop stops the measurement, which is exactly what went wrong the
first time round: it collected nine of the twenty-four hourly snapshots and
then the machine went to sleep.

Before starting, set the laptop to never sleep while plugged in:
  Settings > System > Power & battery > Screen and sleep
  set "When plugged in, put my device to sleep after" to Never

The screen may switch off; that does not matter. Sleep does.


IF IT STOPS ANYWAY
------------------
Nothing is lost. Progress is written after every step, so at most the hour in
progress goes. Run start.bat again — it picks up where it left off.

The one thing worth knowing: a long unwatched gap biases the result downwards.
An arrival nobody was looking for cannot have been caught, so it counts
against the ordering through no fault of the ordering. The report says so when
it spots such a gap. If the gap is large, reset.bat and start over.


THE OTHER TWO FILES
-------------------
report.bat   prints the report from whatever has been collected so far
reset.bat    throws the collected data away and starts afresh


WHAT IT DOES TO THE SHOP
------------------------
Six requests a minute, spaced irregularly — gentler than a person browsing.
About 1,100 requests over the whole day: two full reads of the catalogue at
414 pages between them, and 720 for the hourly head snapshots. It only reads
listing pages; it does not log in, buy, or submit anything.


WHEN IT IS DONE
---------------
Copy measurement.json back and say it is there — or just read the report off
the screen. What matters is the line saying how many arrivals appeared in the
head and the multiple against chance. Above 1.00x the ordering is doing
something; at or below it, the head is no better than reading any other part
of the catalogue.
