from typing import List, Optional
from db.connection import Database
from models import CommandTemplate


class TemplateRepository:
    def __init__(self, db: Database):
        self.db = db

    def list_all(self) -> List[CommandTemplate]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM templates ORDER BY category, name").fetchall()
        return [CommandTemplate(**dict(r)) for r in rows]

    def get(self, template_id: int) -> Optional[CommandTemplate]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
        return CommandTemplate(**dict(row)) if row else None

    def create(self, name: str, category: str, description: str, commands: str) -> int:
        with self.db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO templates (name, category, description, commands) VALUES (?,?,?,?)",
                (name, category, description, commands),
            )
            return cur.lastrowid

    def update(self, template_id: int, name: str, category: str, description: str, commands: str):
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE templates SET name=?, category=?, description=?, commands=? WHERE id=?",
                (name, category, description, commands, template_id),
            )

    def delete(self, template_id: int):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM templates WHERE id=?", (template_id,))
