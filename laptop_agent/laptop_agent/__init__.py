"""JARVIS laptop agent: runs signed file commands inside ~/Downloads, and nothing else.

Outbound only: it polls Supabase and opens no ports. File operations use the
Python standard library only; nothing is ever opened in an app or executed.
"""
