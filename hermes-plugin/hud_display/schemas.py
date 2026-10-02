"""Tool schemas for the Jarvis HUD display plugin.

The description is what makes the agent USE the tool — keep it forceful.
Lesson learned: prose in SOUL.md cannot out-compete an attractive tool schema
(the model kept opening pages in its own invisible browser); a real tool with
an explicit description wins immediately.
"""

HUD_DISPLAY = {
    "name": "hud_display",
    "description": (
        "Display a video, webpage, or image ON THE USER'S SCREEN as a "
        "holographic panel on their Jarvis HUD. ALWAYS use this when the user "
        "asks to show, display, pull up, open, or put any media or webpage "
        "'on screen' or 'on my screen'. This is the ONLY way to show them "
        "visual content - browser tools open pages invisibly and do NOT show "
        "the user anything. YouTube watch/short URLs embed automatically as "
        "playable video. Special URLs: the Hermes kanban board is at "
        "https://YOUR_HOST:9443/kanban and the Hermes dashboard at "
        "https://YOUR_HOST:9443/ (use media=iframe)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "media": {
                "type": "string",
                "enum": ["video", "iframe", "image"],
                "description": "video = YouTube or direct video URL; iframe = any webpage; image = image URL",
            },
            "src": {"type": "string", "description": "Full URL of the video, page, or image"},
            "title": {"type": "string", "description": "Short panel title, e.g. 'ARC REACTOR EXPLAINED'"},
            "position": {
                "type": "string",
                "enum": ["center", "left", "right"],
                "description": "center for one large panel; left/right for smaller side panels when showing multiple things",
            },
        },
        "required": ["media", "src", "title"],
    },
}

HUD_DISMISS = {
    "name": "hud_dismiss",
    "description": "Dismiss all holographic media panels from the user's Jarvis HUD screen. Use when they say to close, clear, or dismiss what's on screen.",
    "parameters": {"type": "object", "properties": {}},
}
