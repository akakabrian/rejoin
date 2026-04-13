Output docs/img/demo.gif
Set Shell bash
Set FontSize 15
Set Width 1280
Set Height 720
Set Theme "Catppuccin Macchiato"
Set TypingSpeed 80ms
Set PlaybackSpeed 0.8

# Launch the TUI but don't record the prompt
Hide
Type "rejoin-tui"
Enter
Sleep 2s
Show

# Now the TUI is alive. Scan sessions.
Sleep 800ms
Type "j"
Sleep 400ms
Type "j"
Sleep 400ms
Type "j"
Sleep 400ms
Type "j"
Sleep 1s

# Search
Type "/"
Sleep 400ms
Type "rejoin"
Sleep 1500ms
Escape
Sleep 500ms

# Navigate to a specific row + show pin
Type "j"
Sleep 500ms
Type "p"
Sleep 600ms
Type "p"
Sleep 400ms

# Theme switch
Type "t"
Sleep 1800ms
Escape
Sleep 500ms

# Quit cleanly
Type "q"
Sleep 400ms
