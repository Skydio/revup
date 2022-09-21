# Script to recreate the animated gifs in the README
# If the output significantly changes, we might want to re-render the gifs
# First record and render with terminalizer
# (with minor changes it might be easier to modify the yml by hand)
# NOTE terminalizer is weirdly nondeterministic. sometimes it will generate
# gifs with the prompt duplicated, so you'll have to regen if that happens.
terminalizer render tutorial_1.yml -o tutorial_1_tmp.gif
terminalizer render tutorial_2.yml -o tutorial_2_tmp.gif
terminalizer render tutorial_3.yml -o tutorial_3_tmp.gif

# Terminalizer generates massive gifs that fill your display so resize them
# Correct sizes found manually and depend on window size in the .yml
# The rough formula for height is 52 + 17 * W
# Uses the latest version of gifsicle https://github.com/kohler/gifsicle
gifsicle --crop 0,0+751x290 --output tutorial_1.gif --colors 256 -O3 tutorial_1_tmp.gif
gifsicle --crop 0,0+751x290 --output tutorial_2.gif --colors 256 -O3 tutorial_2_tmp.gif
gifsicle --crop 0,0+751x172 --output tutorial_3.gif --colors 256 -O3 tutorial_3_tmp.gif

