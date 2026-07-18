from backend.telegram_bot import BOT_COMMANDS, build_telegram_app


def test_bot_commands_shape():
    assert BOT_COMMANDS, "at least one slash command should be registered"
    for entry in BOT_COMMANDS:
        cmd, desc = entry
        assert cmd.islower() and " " not in cmd  # valid Telegram command name
        assert 1 <= len(desc) <= 256


def test_registered_commands_have_handlers():
    # every registered slash command must have a matching CommandHandler
    class _Cfg:
        telegram_bot_token = "123:abc"
        allowed_chat_ids = [1]
    app = build_telegram_app(engine=None, config=_Cfg())
    handler_cmds = set()
    for group in app.handlers.values():
        for h in group:
            cmds = getattr(h, "commands", None)
            if cmds:
                handler_cmds.update(cmds)
    for cmd, _ in BOT_COMMANDS:
        assert cmd in handler_cmds, f"/{cmd} is advertised but has no handler"


def test_custom_command_catch_all_registered_and_hidden():
    # A MessageHandler on COMMAND must exist (the custom-alias catch-all), and it has to be the
    # LAST handler so built-in CommandHandlers claim their commands first. Custom aliases are
    # deliberately absent from BOT_COMMANDS so they don't appear in Telegram's `/` menu.
    from telegram.ext import MessageHandler
    from telegram.ext import filters as tg_filters

    class _Cfg:
        telegram_bot_token = "123:abc"
        allowed_chat_ids = [1]
    app = build_telegram_app(engine=None, config=_Cfg())
    group = app.handlers[0]
    catch_alls = [h for h in group if isinstance(h, MessageHandler) and h.filters is tg_filters.COMMAND]
    assert len(catch_alls) == 1, "expected exactly one COMMAND catch-all for custom aliases"
    assert group[-1] is catch_alls[0], "the catch-all must be registered last so built-ins win"
