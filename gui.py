#!/usr/bin/env python3
"""Single-image GUI with automatic Esri imagery download."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from download_esri_imagery import MAX_TILES, plan_for_osm
from PIL import Image


APP_TITLE = "OSM 建筑轮廓转换工具"
SCRIPT_DIR = Path(__file__).resolve().parent
AUXILIARY_DIR_NAME = "其他文件"
FINAL_OSM_NAME = "最终结果.osm"


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("800x700")
        self.minsize(700, 620)
        self.process: subprocess.Popen[str] | None = None
        self.operation: str | None = None
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.base_osm = tk.StringVar()
        self.output_dir = tk.StringVar(value=str((SCRIPT_DIR / "output" / "area").resolve()))
        self.outlined_image = tk.StringVar()
        self.imagery_status = tk.StringVar(value="尚未下载卫星图")
        self.status = tk.StringVar(value="准备就绪")
        self._build_ui()
        self.after(100, self._poll_events)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=APP_TITLE, font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
        ttk.Label(outer, text="程序自动处理卫星瓦片和坐标；你只需要选择区域、描边图片并转换。", foreground="#555").pack(anchor="w", pady=(3, 16))

        area = ttk.LabelFrame(outer, text="第一步：选择工作区域", padding=12)
        area.pack(fill="x")
        self._path_row(area, 0, "原始 OSM", self.base_osm, self._pick_osm)
        self._path_row(area, 1, "工作文件夹", self.output_dir, self._pick_output)

        download = ttk.LabelFrame(outer, text="第二步：下载卫星图", padding=12)
        download.pack(fill="x", pady=(14, 0))
        ttk.Label(download, text="将根据原始 OSM 的范围自动下载 18 级 Esri World Imagery。", foreground="#555").pack(anchor="w")
        download_row = ttk.Frame(download)
        download_row.pack(fill="x", pady=(10, 0))
        ttk.Label(download_row, textvariable=self.imagery_status).pack(side="left")
        self.download_button = ttk.Button(download_row, text="下载卫星图", command=self._start_download)
        self.download_button.pack(side="right")

        convert = ttk.LabelFrame(outer, text="第三步：选择描边后的图片并转换", padding=12)
        convert.pack(fill="x", pady=(14, 0))
        ttk.Label(convert, text="使用任意图片编辑工具，以洋红色（#FF00FF）描出闭合建筑边界。", foreground="#555").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self._path_row(convert, 1, "描边图片", self.outlined_image, self._pick_image)
        action_row = ttk.Frame(convert)
        action_row.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Button(action_row, text="打开工作文件夹", command=self._open_output).pack(side="left")
        self.run_button = ttk.Button(action_row, text="转换为 OSM", command=self._start_conversion)
        self.run_button.pack(side="right")

        logs = ttk.LabelFrame(outer, text="运行信息", padding=8)
        logs.pack(fill="both", expand=True, pady=(14, 0))
        self.log = tk.Text(logs, height=11, wrap="word", state="disabled", font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(logs, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        ttk.Label(outer, textvariable=self.status).pack(anchor="w", pady=(7, 0))

    @staticmethod
    def _path_row(parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, command) -> None:
        ttk.Label(parent, text=label, width=12).grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8, pady=5)
        ttk.Button(parent, text="浏览…", command=command).grid(row=row, column=2, pady=5)
        parent.columnconfigure(1, weight=1)

    def _pick_osm(self) -> None:
        value = filedialog.askopenfilename(parent=self, title="选择原始 OSM 文件", filetypes=[("OSM 文件", "*.osm"), ("所有文件", "*.*")])
        if value:
            self.base_osm.set(value)
            self._update_plan_text()

    def _pick_output(self) -> None:
        value = filedialog.askdirectory(parent=self, title="选择工作文件夹")
        if value:
            self.output_dir.set(value)
            self._detect_existing_metadata()

    def _pick_image(self) -> None:
        value = filedialog.askopenfilename(parent=self, title="选择描边图片", filetypes=[("图片", "*.png *.jpg *.jpeg *.tif *.tiff"), ("所有文件", "*.*")])
        if value:
            self.outlined_image.set(value)

    def _valid_paths(self) -> tuple[Path, Path]:
        osm = Path(self.base_osm.get().strip())
        output_text = self.output_dir.get().strip()
        if not osm.is_file():
            raise ValueError("请先选择有效的原始 OSM 文件。")
        if not output_text:
            raise ValueError("请选择工作文件夹。")
        return osm.resolve(), Path(output_text).resolve()

    def _update_plan_text(self) -> dict | None:
        try:
            osm, _ = self._valid_paths()
            plan = plan_for_osm(osm, 18)
            self.imagery_status.set(f"预计 {plan['tiles_wide']}×{plan['tiles_high']} 张，图片 {plan['pixel_width']}×{plan['pixel_height']} 像素")
            return plan
        except (OSError, ValueError) as exc:
            self.imagery_status.set(str(exc))
            return None

    def _start_download(self) -> None:
        try:
            osm, output = self._valid_paths()
            plan = plan_for_osm(osm, 18)
            if plan["tile_count"] > MAX_TILES:
                raise ValueError(f"范围包含 {plan['tile_count']} 张瓦片，超过上限 {MAX_TILES}。请在 JOSM 中选择更小范围。")
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法下载", str(exc), parent=self)
            return
        prompt = f"将下载 {plan['tiles_wide']}×{plan['tiles_high']} 张瓦片，生成 {plan['pixel_width']}×{plan['pixel_height']} 图片。\n\n继续吗？"
        if not messagebox.askyesno("确认下载", prompt, parent=self):
            return
        output.mkdir(parents=True, exist_ok=True)
        self._set_busy(True, "正在下载卫星图…", "download")
        self._write_log("\n开始下载 Esri World Imagery。\n")
        auxiliary = output / AUXILIARY_DIR_NAME
        command = [sys.executable, "-u", str(SCRIPT_DIR / "download_esri_imagery.py"), str(osm), str(auxiliary), "--zoom", "18"]
        threading.Thread(target=self._worker, args=(command,), daemon=True).start()

    def _metadata(self) -> tuple[Path, dict]:
        image = Path(self.outlined_image.get().strip())
        if not image.is_file():
            raise ValueError("请选择有效的描边图片。")
        _, output = self._valid_paths()
        candidates = [output / AUXILIARY_DIR_NAME / "imagery-info.json", output / "imagery-info.json", image.parent / "imagery-info.json"]
        for path in candidates:
            if path.is_file():
                return path, json.loads(path.read_text(encoding="utf-8"))
        raise ValueError("找不到卫星图定位信息。请先使用本工具下载卫星图，并保留 imagery-info.json。")

    def _start_conversion(self) -> None:
        try:
            osm, output = self._valid_paths()
            _, metadata = self._metadata()
            image = Path(self.outlined_image.get().strip()).resolve()
            required = ("x", "y", "z", "tiles_wide", "tiles_high")
            if any(key not in metadata for key in required):
                raise ValueError("imagery-info.json 内容不完整，请重新下载卫星图。")
            with Image.open(image) as opened:
                expected = (int(metadata["pixel_width"]), int(metadata["pixel_height"]))
                expected_ratio = expected[0] / expected[1]
                actual_ratio = opened.width / opened.height
                if abs(actual_ratio / expected_ratio - 1.0) > 0.005:
                    raise ValueError(
                        f"描边图片尺寸为 {opened.width}×{opened.height}，应保持原图 "
                        f"{expected[0]}×{expected[1]} 的宽高比。图片可能被裁剪或变形。"
                    )
            config = {
                "base_osm": str(osm), "output_dir": str(output),
                "chunks": [{"name": "image", "image": str(image), **{key: metadata[key] for key in required}}],
                "vectorize": {"min_area": 50, "simplify": 4.0, "edge_margin": 8, "dilate": 1, "color_mode": "strict", "orientation_radius": 120.0, "orientation_tolerance": 8.0},
                "overlap_resolution": {"max_overlap_m": 1.5, "gap_m": 0.12, "orientation_tolerance": 6.0},
            }
            output.mkdir(parents=True, exist_ok=True)
            auxiliary = output / AUXILIARY_DIR_NAME
            auxiliary.mkdir(parents=True, exist_ok=True)
            manifest = auxiliary / "运行配置.json"
            manifest.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            messagebox.showerror("无法转换", str(exc), parent=self)
            return
        self._set_busy(True, "正在转换，请不要关闭窗口…", "convert")
        self._write_log("\n开始转换描边图片。\n")
        command = [sys.executable, "-u", str(SCRIPT_DIR / "run_postprocess.py"), str(manifest)]
        threading.Thread(target=self._worker, args=(command,), daemon=True).start()

    def _worker(self, command: list[str]) -> None:
        try:
            self.process = subprocess.Popen(command, cwd=SCRIPT_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.events.put(("log", line))
            self.events.put(("done", (self.operation, self.process.wait())))
        except Exception as exc:
            self.events.put(("error", str(exc)))
        finally:
            self.process = None

    def _poll_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self._write_log(str(value))
                elif kind == "done":
                    operation, code = value
                    self._set_busy(False, "准备就绪", None)
                    if code != 0:
                        self.status.set("操作失败，请查看运行信息。")
                        messagebox.showerror("操作失败", "请查看窗口下方的运行信息。", parent=self)
                    elif operation == "download":
                        _, output = self._valid_paths()
                        image = output / AUXILIARY_DIR_NAME / "esri-imagery-z18.jpg"
                        self.imagery_status.set(f"下载完成：{image.name}")
                        self.status.set("卫星图下载完成，可以使用图片编辑工具描边。")
                        messagebox.showinfo(
                            "下载完成",
                            f"卫星图已保存：\n{image}\n\n请使用洋红色（#FF00FF）描出闭合建筑边界。"
                            "\n不要裁剪或改变图片宽高比。",
                            parent=self,
                        )
                    else:
                        result = Path(self.output_dir.get()) / FINAL_OSM_NAME
                        self.status.set("转换完成，可以用 JOSM 打开结果。")
                        messagebox.showinfo("转换完成", f"结果已生成：\n{result}\n\n请在 JOSM 中逐栋复核。", parent=self)
                elif kind == "error":
                    self._set_busy(False, "无法启动操作。", None)
                    self._write_log(f"启动失败：{value}\n")
                    messagebox.showerror("启动失败", str(value), parent=self)
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _set_busy(self, busy: bool, status: str, operation: str | None) -> None:
        state = "disabled" if busy else "normal"
        self.download_button.configure(state=state)
        self.run_button.configure(state=state)
        self.operation = operation
        self.status.set(status)

    def _detect_existing_metadata(self) -> None:
        value = self.output_dir.get().strip()
        if value and any((Path(value) / relative).is_file() for relative in (Path(AUXILIARY_DIR_NAME) / "imagery-info.json", Path("imagery-info.json"))):
            self.imagery_status.set("已找到之前下载的卫星图定位信息")

    def _write_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _open_output(self) -> None:
        value = self.output_dir.get().strip()
        if value:
            open_folder(Path(value).resolve())

    def _close(self) -> None:
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno("仍在运行", "操作尚未完成，确定要关闭吗？", parent=self):
                return
            self.process.terminate()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
