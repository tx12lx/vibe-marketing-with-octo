from __future__ import annotations

import click

from commands.analyze import analyze
from commands.ask import ask
from commands.chat import chat
from commands.feedback import feedback, feedback_summary
from commands.learn import learn
from commands.sql import sql
from commands.validate import validate


@click.group()
@click.version_option(version="1.0.0", prog_name="vibe-briefing")
def cli():
    """Vibe Briefing — campaign intelligence brain.

    Learns from historical data briefs, translates business language into
    structured JSON payloads, and routes natural language questions to the
    right downstream tool (sizing, brief regeneration, brief assistant).

    \b
    Core workflow:
      1. Learn from briefs:
           vibe-briefing learn --campaign-name "AAL Monthly EM" --bq-table ... --output glossary/aal.json
      2. Ask natural language questions:
           vibe-briefing ask "how many customers qualify for TELUS AAL email?"
      3. Generate new briefs:
           vibe-briefing chat --prompt "..." --glossary glossary/aal.json
      4. Submit feedback to train the brain:
           vibe-briefing feedback --campaign "AAL Monthly EM" --tool sizing_tool --row-count 145000

    \b
    Scaling:
      - Add BQ field mappings to config/bq_schema.json to unlock SQL generation
      - Run learn on more portfolios to expand the knowledge base
      - Feedback loop accumulates in feedback_log inside each brief JSON
    """


cli.add_command(learn)
cli.add_command(ask)
cli.add_command(chat)
cli.add_command(feedback)
cli.add_command(feedback_summary)
cli.add_command(analyze)
cli.add_command(validate)
cli.add_command(sql)


if __name__ == "__main__":
    cli()
