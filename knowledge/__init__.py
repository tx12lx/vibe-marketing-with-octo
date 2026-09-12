"""knowledge -- the tool's own memory: table/column knowledge, glossary, business
rules, and a permanent record of every confirmation and correction.

Built from the ground up to replace the deleted legacy knowledge layer. Lives as
one self-checking SQLite database file for now (see knowledge/store.py); the same
schema moves into a BigQuery dataset later without a redesign.
"""
