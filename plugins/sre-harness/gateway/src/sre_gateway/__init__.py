"""Chat-ops gateway for the sre-harness plugin.

A thin bridge: it relays chat messages to an agent session and the agent's
replies back, turns the agent's permission prompts into approval requests,
and records approvers' decisions in the incident's audit log. It holds no
model logic and opens no inbound port.
"""

__version__ = "0.8.0"
