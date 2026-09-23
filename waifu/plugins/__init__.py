"""The command layer: one module per feature area, registered by :mod:`waifu.core.dp`.

Layout rules, so this directory does not become what we criticised in the bot it
replaces (one 109 KB ``commands_user.py`` where every command reached into the
database directly):

* a module owns its **commands and callbacks**, nothing else;
* all business rules live in :mod:`waifu.services`, so a rule can be reused by the
  scheduler, the CLI and a test without importing aiogram;
* no SQL here — the ``session`` the middleware injects is handed to a service;
* one callback namespace per module (``col:``, ``gacha:``…), which is what stops the
  27 overlapping prefixes of the legacy bot from stealing each other's presses.

Add a feature by copying ``_template.py``; ``waifu doctor`` reports any module that
fails to import instead of the bot silently losing commands.
"""
