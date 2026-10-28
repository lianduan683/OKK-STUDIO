"""Whole-session project lifecycle adapted from the companion exporter."""
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox
from project_file import (AUTOSAVE_INTERVAL_MS, PROJECT_SUFFIX, ProjectFileError, apply_state, autosave_path, build_project_payload, collect_state, load_recent_projects, projects_dir, read_project_file, record_recent_project, state_digest, write_project_file)
APP_NAME = 'OKK STUDIO'
APP_VERSION = '5.12.1'
def app_directory():
    import sys
    return Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
class StudioProjectMixin:
    def _init_project_state(self) -> None:
        self.project_path: Path | None = None
        self._project_saved_digest = ""
        self._project_autosave_digest = ""
        self._project_default_state = collect_state(self)
        self._project_saved_digest = self._project_current_digest() or ""
        self._project_autosave_digest = self._project_saved_digest
        self._project_refresh_recent_menu()
        self._project_update_title()


    def _project_refresh_recent_menu(self) -> None:
        menu = getattr(self, "_project_recent_menu", None)
        if menu is None:
            return
        menu.delete(0, "end")
        items = load_recent_projects(app_directory())
        if not items:
            menu.add_command(label="（暂无）", state="disabled")
            return
        for item in items:
            menu.add_command(
                label=f"{item['name']}  —  {item['saved_at']}",
                command=lambda path=item["path"]: self._project_open(path),
            )


    def _project_current_digest(self) -> str | None:
        try:
            return state_digest(collect_state(self))
        except Exception:
            return None


    def _project_is_dirty(self) -> bool:
        digest = self._project_current_digest()
        # An invalid in-progress field must never suppress the save prompt.
        return digest is None or digest != self._project_saved_digest


    def _project_update_title(self) -> None:
        if self._project_launcher is not None:
            self.title(f"选择工作项目 — {APP_NAME} {APP_VERSION}")
            return
        name = self.project_path.stem if self.project_path else "未命名工程"
        star = " *" if self._project_is_dirty() else ""
        self.title(f"{name}{star} — {APP_NAME} {APP_VERSION}")


    def _project_confirm_discard(self) -> bool:
        if not self._project_is_dirty():
            return True
        name = self.project_path.stem if self.project_path else "未命名工程"
        answer = messagebox.askyesnocancel(
            APP_NAME, f"工程“{name}”有未保存的修改。\n\n保存修改吗？"
        )
        if answer is None:
            return False
        if answer:
            return self._project_save()
        return True


    def _project_switch_allowed(self, action: str) -> bool:
        if self.working or self.multicam_workspace.busy:
            messagebox.showinfo(
                APP_NAME, f"{action}进行中，请等待完成或取消任务后再切换工程。",
            )
            return False
        return True


    def _project_new(self) -> None:
        if not self._project_switch_allowed("任务"):
            return
        if not self._project_confirm_discard():
            return
        warnings = apply_state(self, {"state": self._project_default_state})
        self.project_path = None
        self._project_saved_gpu_settings = False
        self._project_after_state_applied(warnings, log="已新建工程。")


    def _project_open(self, path=None) -> None:
        if not self._project_switch_allowed("任务"):
            return
        if path is None:
            path = filedialog.askopenfilename(
                title="打开工程",
                initialdir=str(projects_dir(app_directory())),
                filetypes=[("RTX 工程文件", f"*{PROJECT_SUFFIX}")],
            )
        if not path:
            return
        if not self._project_confirm_discard():
            return
        try:
            data = read_project_file(path)
        except ProjectFileError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        try:
            warnings = apply_state(self, data)
        except ProjectFileError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.project_path = Path(path)
        self._project_saved_gpu_settings = True
        self._project_after_state_applied(warnings, log=f"工程已打开：{path}")
        record_recent_project(app_directory(), path)
        self._project_refresh_recent_menu()


    def _project_save(self) -> bool:
        if self.project_path is None:
            return self._project_save_as()
        return self._project_write(self.project_path)


    def _project_save_as(self) -> bool:
        initial_file = (
            f"{self.project_path.stem}{PROJECT_SUFFIX}" if self.project_path
            else f"未命名工程{PROJECT_SUFFIX}"
        )
        path = filedialog.asksaveasfilename(
            title="工程另存为",
            initialdir=str(projects_dir(app_directory())),
            initialfile=initial_file,
            defaultextension=PROJECT_SUFFIX,
            filetypes=[("RTX 工程文件", f"*{PROJECT_SUFFIX}")],
        )
        if not path:
            return False
        return self._project_write(Path(path))


    def _project_write(self, path: Path) -> bool:
        try:
            payload = build_project_payload(self, project_path=str(path), app_version=APP_VERSION)
            write_project_file(path, payload)
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror(APP_NAME, f"工程保存失败：{exc}")
            return False
        self.project_path = Path(path)
        self.multicam_workspace.dirty = False
        record_recent_project(app_directory(), path)
        self._project_saved_digest = state_digest(payload["state"])
        self._project_refresh_recent_menu()
        self._write_project_autosave()
        self._project_update_title()
        self._log(f"工程已保存：{path}")
        return True


    def _write_project_autosave(self, *, notify: bool = False) -> bool:
        try:
            payload = build_project_payload(
                self, project_path=str(self.project_path) if self.project_path else "",
                app_version=APP_VERSION,
            )
            write_project_file(autosave_path(app_directory()), payload)
        except (OSError, ValueError, TypeError) as exc:
            self._autosave_error = str(exc)
            self._log(f"自动保存失败：{exc}")
            if notify:
                messagebox.showwarning(APP_NAME, f"自动保存失败：{exc}")
            return False
        self._autosave_error = ""
        self._project_autosave_digest = state_digest(payload["state"])
        return True


    def _project_autosave_tick(self) -> None:
        if getattr(self, "_closing", False):
            return
        digest = self._project_current_digest()
        if digest and digest != self._project_autosave_digest:
            self._write_project_autosave()
        self._project_update_title()
        self.after(AUTOSAVE_INTERVAL_MS, self._project_autosave_tick)


    def _project_restore_autosave(self) -> None:
        if not self._project_switch_allowed("任务"):
            return
        path = autosave_path(app_directory())
        try:
            data = read_project_file(path)
        except ProjectFileError as exc:
            messagebox.showinfo(APP_NAME, f"没有可恢复的会话：{exc}")
            return
        if not self._project_confirm_discard():
            return
        self._project_apply_payload(data, restore_path=True)


    @staticmethod
    def _resolve_project_link(linked: str) -> Path | None:
        if not linked:
            return None
        target = Path(linked)
        if not target.is_absolute():
            target = app_directory() / target
        try:
            return target.resolve()
        except OSError:
            return target


    def _project_apply_payload(self, data, *, restore_path: bool) -> None:
        try:
            warnings = apply_state(self, data)
        except ProjectFileError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self._project_saved_gpu_settings = True
        saved_digest: str | None = None
        linked_text = str(data.get("project_path") or "")
        if restore_path:
            linked = self._resolve_project_link(linked_text)
            if linked is not None:
                if linked.is_file():
                    self.project_path = linked
                    try:
                        saved = read_project_file(linked)
                    except ProjectFileError as exc:
                        warnings.append(f"链接的工程文件无法读取：{exc}")
                        saved_digest = ""
                    else:
                        # 恢复的是自动保存的现场状态；只有与工程文件保存内容
                        # 一致时才清除脏标记，否则保留“未保存修改”。
                        saved_state = saved["state"]
                        if isinstance(saved_state.get("join"), dict):
                            saved_state["join"].setdefault("overlay_assets", {})
                        saved_digest = state_digest(saved_state)
                else:
                    warnings.append(f"链接的工程文件不存在：{linked}")
                    self.project_path = None
                    saved_digest = ""
            else:
                self.project_path = None
                # An unnamed recovery has never been saved as a project.
                # Compare with the initial blank state, not the recovered state,
                # so switching projects still offers to save recovered edits.
                saved_digest = state_digest(getattr(self, "_project_default_state", {}))
        self._project_after_state_applied(
            warnings, log="已恢复上次会话。", saved_digest=saved_digest,
        )


    def _project_after_state_applied(
        self, warnings, *, log: str, saved_digest: str | None = None,
    ) -> None:
        if saved_digest is None:
            self._project_saved_digest = self._project_current_digest() or ""
        else:
            self._project_saved_digest = saved_digest
        self._write_project_autosave()
        self._project_update_title()
        self._log(log)
        if warnings:
            self._log("工程恢复提示：" + "；".join(warnings))
            messagebox.showwarning(
                APP_NAME,
                "工程已恢复，但部分素材需要重新链接：\n\n" + "\n".join(warnings),
            )

