"""把 i学习 作业的存储目录从数字 id 改成「班级-作业名」。

改之前：objects/istudy/works/9/students/250410001/…
改之后：objects/istudy/works/25人工智能本1-第1周作业/students/250410001/…

会同时把数据库里 istudy_submissions / istudy_submission_files / istudy_exports
的 object_key 一起改掉，所以改完功能不受影响。

默认只预览，想真改加 --apply：
    PYTHONPATH=. ./.venv/Scripts/python.exe scripts/rename_istudy_folders.py --apply
"""

from __future__ import annotations

import sys

from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import IstudyExport, IstudySubmission, IstudySubmissionFile, IstudyWork
from app.services.homework_sync import work_folder
from app.storage import get_storage


def rewrite(db, model, old_prefix: str, new_prefix: str) -> int:
    """把某张表里以 old_prefix 开头的 object_key 换成 new_prefix。"""
    changed = 0
    for row in db.execute(select(model).where(model.object_key.is_not(None))).scalars():
        if row.object_key and row.object_key.startswith(old_prefix + "/"):
            row.object_key = new_prefix + row.object_key[len(old_prefix) :]
            changed += 1
    return changed


def main() -> int:
    apply = "--apply" in sys.argv
    init_db()
    storage = get_storage()

    with SessionLocal() as db:
        works = list(db.execute(select(IstudyWork).order_by(IstudyWork.id)).scalars())
        plans: list[tuple[IstudyWork, str, str]] = []
        seen: dict[str, int] = {}
        for work in works:
            folder = work_folder(work)
            if folder in seen:
                print(
                    f"⚠️  work#{work.id} 和 work#{seen[folder]} 会算出同一个目录名「{folder}」，"
                    "给它加上 i学习 workId 区分"
                )
                folder = f"{folder}-{work.istudy_work_id}"
            seen[folder] = work.id

            old = f"istudy/works/{work.id}"
            new = f"istudy/works/{folder}"
            if old == new:
                continue
            plans.append((work, old, new))

        if not plans:
            print("没有需要改名的目录")
            return 0

        total = 0
        for work, old, new in plans:
            print(f"  {old}  →  {new}")
            if not apply:
                continue
            moved = storage.move_prefix(old, new) if storage.exists(old) else 0
            total += moved
            counts = (
                rewrite(db, IstudySubmission, old, new)
                + rewrite(db, IstudySubmissionFile, old, new)
                + rewrite(db, IstudyExport, old, new)
            )
            note = "（目录之前已经挪过，只补了数据库）" if moved == 0 else ""
            print(f"      挪了 {moved} 个文件，更新了 {counts} 条 object_key {note}")

        if apply:
            db.commit()
            print(f"\n完成：{len(plans)} 个目录，{total} 个文件")
        else:
            print(f"\n以上是预览，加 --apply 才会真的改（共 {len(plans)} 个目录）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
