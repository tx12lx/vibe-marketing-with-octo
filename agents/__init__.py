# agents/ — pluggable agent registry directory.
#
# Each .py file here that defines a BaseAgent subclass is auto-discovered at
# orchestrator startup by _discover_agents().  Adding a new agent requires only
# dropping one file here — zero changes to vibe_orchestrator.py.
#
# Current registered agents:
#   nexus_agent.py    — NexusAgent (routing, intent classification, general_question)
#   quant_agent.py    — QuantAgent (SQL generation, audience sizing)
#   feedback_agent.py — FeedbackAgent (correction interpretation, rule extraction)
