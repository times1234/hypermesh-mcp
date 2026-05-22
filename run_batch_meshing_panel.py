"""Standalone Windows panel for background HyperMesh meshing."""

from __future__ import annotations

import subprocess
import sys
import time
import tkinter as tk
import json
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT / "runs"
OUTPUTS_DIR = ROOT / "outputs"


class BatchMeshingPanel:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.process: subprocess.Popen[str] | None = None
        self.log_path: Path | None = None
        self.current_stamp: str | None = None
        self.log_pos = 0

        self.vars: dict[str, tk.Variable] = {
            "input": tk.StringVar(value=""),
            "output": tk.StringVar(value=str(OUTPUTS_DIR / "full_mesh_batch.hm")),
            "hmbatch": tk.StringVar(value=""),
            "drag_element_size_min": tk.StringVar(value="0.5"),
            "drag_element_size_max": tk.StringVar(value="1.5"),
            "tetra_element_size_min": tk.StringVar(value="1.5"),
            "tetra_element_size_max": tk.StringVar(value="2.0"),
            "tetra_min_element_size_min": tk.StringVar(value="0.4"),
            "tetra_min_element_size_max": tk.StringVar(value="0.6"),
            "spin_density_min": tk.StringVar(value="60"),
            "spin_density_max": tk.StringVar(value="160"),
            "spin_section_element_size_min": tk.StringVar(value="0.20"),
            "spin_section_element_size_max": tk.StringVar(value="1.50"),
            "drag_fit_tolerance_ratio": tk.StringVar(value="0.05"),
            "drag_retry_count": tk.StringVar(value="2"),
            "tetra_max_deviation": tk.StringVar(value="0.1"),
            "tetra_feature_angle": tk.StringVar(value="30"),
            "tetra_growth_rate": tk.StringVar(value="1.23"),
            "tetra_fit_tolerance_ratio": tk.StringVar(value="0.01"),
            "tetra_chord_dev_degrade_delta": tk.StringVar(value="0.20"),
            "tetra_target_vol_skew": tk.StringVar(value="0.70"),
            "tetra_repair_vol_skew": tk.StringVar(value="0.99"),
            "gear_tooth_element_size_min": tk.StringVar(value="1.2"),
            "gear_tooth_element_size_max": tk.StringVar(value="1.6"),
            "gear_tooth_min_element_size_min": tk.StringVar(value="0.2"),
            "gear_tooth_min_element_size_max": tk.StringVar(value="0.3"),
            "gear_tooth_feature_angle": tk.StringVar(value="15"),
            "use_gear_tooth_refinement": tk.BooleanVar(value=True),
            "drag_aspect_guard": tk.BooleanVar(value=True),
            "continue_on_error": tk.BooleanVar(value=True),
        }
        self.status = tk.StringVar(value="状态：等待选择模型")
        self._build_ui()

    def _build_ui(self) -> None:
        self.root.title("HyperMesh 后台自动网格划分")
        self.root.geometry("1080x900+120+60")
        self.root.minsize(980, 700)

        style = ttk.Style()
        for theme in ("vista", "xpnative", "winnative", "default"):
            if theme in style.theme_names():
                try:
                    style.theme_use(theme)
                    break
                except tk.TclError:
                    continue
        style.configure("Root.TFrame", background="#edf1f7")
        style.configure("Card.TLabelframe", background="#ffffff", padding=12)
        style.configure("Card.TLabelframe.Label", font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Title.TLabel", background="#edf1f7", foreground="#172033", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Sub.TLabel", background="#edf1f7", foreground="#5c6675", font=("Microsoft YaHei UI", 10))
        style.configure("Status.TLabel", background="#dbeafe", foreground="#17324d", font=("Microsoft YaHei UI", 11, "bold"), padding=(10, 6))
        style.configure("TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(10, 5))
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TEntry", padding=4)

        root = ttk.Frame(self.root, padding=16, style="Root.TFrame")
        root.grid(row=0, column=0, sticky="nsew")
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(5, weight=1)

        ttk.Label(root, text="HyperMesh 后台自动网格划分面板", style="Title.TLabel").grid(row=0, column=0, sticky="ew")
        ttk.Label(root, text="选择 STP/STEP/HM 模型后，程序会自动调用 HyperMesh 后台批处理划分；运行时可继续操作可视 HyperMesh。", style="Sub.TLabel").grid(row=1, column=0, sticky="ew", pady=(2, 8))
        ttk.Label(root, textvariable=self.status, style="Status.TLabel", anchor="w").grid(row=2, column=0, sticky="ew", pady=(0, 10))

        main = ttk.Frame(root, style="Root.TFrame")
        main.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        main.grid_columnconfigure(0, weight=1)
        main.grid_columnconfigure(1, weight=1)

        left = ttk.Labelframe(main, text="项目和尺寸限制", style="Card.TLabelframe")
        right = ttk.Labelframe(main, text="质量和齿面参数", style="Card.TLabelframe")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        left.grid_columnconfigure(1, weight=1)
        right.grid_columnconfigure(1, weight=1)

        self._file_row(left, 0, "输入模型", "input", self._browse_input)
        self._file_row(left, 1, "输出 HM", "output", self._browse_output)
        self._entry_row(left, 4, "drag 尺寸下限", "drag_element_size_min")
        self._entry_row(left, 5, "drag 尺寸上限", "drag_element_size_max")
        self._entry_row(left, 6, "tetra 目标下限", "tetra_element_size_min")
        self._entry_row(left, 7, "tetra 目标上限", "tetra_element_size_max")
        self._entry_row(left, 8, "tetra 最小下限", "tetra_min_element_size_min")
        self._entry_row(left, 9, "tetra 最小上限", "tetra_min_element_size_max")
        self._entry_row(left, 10, "spin 份数下限", "spin_density_min")
        self._entry_row(left, 11, "spin 份数上限", "spin_density_max")
        self._entry_row(left, 12, "spin section size min", "spin_section_element_size_min")
        self._entry_row(left, 13, "spin section size max", "spin_section_element_size_max")

        ttk.Checkbutton(right, text="drag 三层", variable=self.vars["drag_aspect_guard"]).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Checkbutton(right, text="启用齿面加厚/加密模型", variable=self.vars["use_gear_tooth_refinement"]).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Checkbutton(right, text="失败后继续后续批次", variable=self.vars["continue_on_error"]).grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self._entry_row(right, 3, "drag 贴合比例", "drag_fit_tolerance_ratio")
        self._entry_row(right, 4, "drag 重试次数", "drag_retry_count")
        self._entry_row(right, 5, "tetra 最大偏差", "tetra_max_deviation")
        self._entry_row(right, 6, "tetra 特征角", "tetra_feature_angle")
        self._entry_row(right, 7, "tetra 增长率", "tetra_growth_rate")
        self._entry_row(right, 8, "tetra 贴合比例", "tetra_fit_tolerance_ratio")
        self._entry_row(right, 9, "最大 chord dev 下降值", "tetra_chord_dev_degrade_delta")
        self._entry_row(right, 10, "目标 vol skew", "tetra_target_vol_skew")
        self._entry_row(right, 11, "修复 vol skew", "tetra_repair_vol_skew")
        self._entry_row(right, 12, "齿面 tetra 下限", "gear_tooth_element_size_min")
        self._entry_row(right, 13, "齿面 tetra 上限", "gear_tooth_element_size_max")
        self._entry_row(right, 14, "齿面最小下限", "gear_tooth_min_element_size_min")
        self._entry_row(right, 15, "齿面最小上限", "gear_tooth_min_element_size_max")
        self._entry_row(right, 16, "齿面特征角", "gear_tooth_feature_angle")

        logbox = ttk.Labelframe(root, text="运行日志", style="Card.TLabelframe")
        logbox.grid(row=5, column=0, sticky="nsew", pady=(0, 10))
        logbox.grid_rowconfigure(0, weight=1)
        logbox.grid_columnconfigure(0, weight=1)
        self.log_text = tk.Text(logbox, height=10, wrap="none", state="disabled", font=("Consolas", 10), background="#101828", foreground="#e5e7eb", relief="flat")
        yscroll = ttk.Scrollbar(logbox, orient="vertical", command=self.log_text.yview)
        xscroll = ttk.Scrollbar(logbox, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")

        actions = ttk.Frame(root, style="Root.TFrame")
        actions.grid(row=6, column=0, sticky="ew")
        ttk.Button(actions, text="开始划分", command=self.start).grid(row=0, column=0, padx=(0, 12))
        ttk.Button(actions, text="停止", command=self.stop).grid(row=0, column=1, padx=12)

    def _entry_row(self, parent: ttk.Widget, row: int, label: str, key: str) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="ew", padx=(0, 10), pady=4)
        ttk.Entry(parent, textvariable=self.vars[key], width=16).grid(row=row, column=1, sticky="ew", pady=4)

    def _file_row(self, parent: ttk.Widget, row: int, label: str, key: str, command) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="ew", padx=(0, 10), pady=5)
        ttk.Entry(parent, textvariable=self.vars[key]).grid(row=row, column=1, sticky="ew", pady=5)
        ttk.Button(parent, text="浏览", command=command).grid(row=row, column=2, sticky="ew", padx=(8, 0), pady=5)

    def _browse_input(self) -> None:
        picked = filedialog.askopenfilename(
            title="选择输入模型",
            filetypes=[("模型文件", "*.stp *.step *.hm"), ("STEP files", "*.stp *.step"), ("HyperMesh files", "*.hm"), ("All files", "*.*")],
        )
        if picked:
            self.vars["input"].set(str(Path(picked).resolve()))
            if not self.vars["output"].get() or self.vars["output"].get().endswith("full_mesh_batch.hm"):
                self.vars["output"].set(str((OUTPUTS_DIR / f"{Path(picked).stem}_mesh.hm").resolve()))

    def _browse_output(self) -> None:
        picked = filedialog.asksaveasfilename(
            title="选择输出 HM 文件",
            defaultextension=".hm",
            filetypes=[("HyperMesh files", "*.hm"), ("All files", "*.*")],
        )
        if picked:
            self.vars["output"].set(str(Path(picked).resolve()))

    def _browse_hmbatch(self) -> None:
        picked = filedialog.askopenfilename(title="选择 hmbatch.exe", filetypes=[("hmbatch", "hmbatch.exe"), ("Executable", "*.exe"), ("All files", "*.*")])
        if picked:
            self.vars["hmbatch"].set(str(Path(picked).resolve()))

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _replace_log(self, text: str = "") -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        if text:
            self.log_text.insert("end", text)
        self.log_text.configure(state="disabled")

    def _build_command(self, stamp: str) -> list[str]:
        runner = ROOT / "run_full_meshing_workflow_batch.py"
        cmd = [
            sys.executable,
            str(runner),
            "--input",
            self.vars["input"].get(),
            "--output",
            self.vars["output"].get(),
            "--stamp",
            stamp,
            "--write-json",
            "--drag-element-size",
            self.vars["drag_element_size_max"].get(),
            "--drag-element-size-min",
            self.vars["drag_element_size_min"].get(),
            "--drag-element-size-max",
            self.vars["drag_element_size_max"].get(),
            "--drag-fit-tolerance-ratio",
            self.vars["drag_fit_tolerance_ratio"].get(),
            "--drag-retry-count",
            self.vars["drag_retry_count"].get(),
            "--spin-section-element-size-min",
            self.vars["spin_section_element_size_min"].get(),
            "--spin-section-element-size-max",
            self.vars["spin_section_element_size_max"].get(),
            "--spin-density-min",
            self.vars["spin_density_min"].get(),
            "--spin-density-max",
            self.vars["spin_density_max"].get(),
            "--tetra-element-size",
            self.vars["tetra_element_size_max"].get(),
            "--tetra-element-size-min",
            self.vars["tetra_element_size_min"].get(),
            "--tetra-element-size-max",
            self.vars["tetra_element_size_max"].get(),
            "--tetra-min-element-size",
            self.vars["tetra_min_element_size_max"].get(),
            "--tetra-min-element-size-min",
            self.vars["tetra_min_element_size_min"].get(),
            "--tetra-min-element-size-max",
            self.vars["tetra_min_element_size_max"].get(),
            "--tetra-max-deviation",
            self.vars["tetra_max_deviation"].get(),
            "--tetra-feature-angle",
            self.vars["tetra_feature_angle"].get(),
            "--tetra-growth-rate",
            self.vars["tetra_growth_rate"].get(),
            "--tetra-fit-tolerance-ratio",
            self.vars["tetra_fit_tolerance_ratio"].get(),
            "--tetra-chord-dev-degrade-delta",
            self.vars["tetra_chord_dev_degrade_delta"].get(),
            "--tetra-target-vol-skew",
            self.vars["tetra_target_vol_skew"].get(),
            "--tetra-repair-vol-skew",
            self.vars["tetra_repair_vol_skew"].get(),
            "--gear-tooth-element-size",
            self.vars["gear_tooth_element_size_max"].get(),
            "--gear-tooth-element-size-min",
            self.vars["gear_tooth_element_size_min"].get(),
            "--gear-tooth-element-size-max",
            self.vars["gear_tooth_element_size_max"].get(),
            "--gear-tooth-min-element-size",
            self.vars["gear_tooth_min_element_size_max"].get(),
            "--gear-tooth-min-element-size-min",
            self.vars["gear_tooth_min_element_size_min"].get(),
            "--gear-tooth-min-element-size-max",
            self.vars["gear_tooth_min_element_size_max"].get(),
            "--gear-tooth-feature-angle",
            self.vars["gear_tooth_feature_angle"].get(),
        ]
        hmbatch = self.vars["hmbatch"].get().strip()
        if hmbatch:
            cmd.extend(["--hmbatch", hmbatch])
        cmd.append("--continue-on-error" if self.vars["continue_on_error"].get() else "--stop-on-error")
        if self.vars["drag_aspect_guard"].get():
            cmd.append("--drag-aspect-guard")
        cmd.append("--use-gear-tooth-refinement" if self.vars["use_gear_tooth_refinement"].get() else "--no-gear-tooth-refinement")
        return cmd

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showwarning("正在运行", "当前已有后台划分流程在运行。")
            return
        input_path = Path(self.vars["input"].get())
        if not input_path.exists():
            messagebox.showerror("输入无效", "请选择存在的 STP/STEP/HM 模型文件。")
            return
        OUTPUTS_DIR.mkdir(exist_ok=True)
        RUNS_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.current_stamp = stamp
        self.log_path = RUNS_DIR / f"batch_panel_{stamp}.log"
        self.log_pos = 0
        self._replace_log("")
        cmd = self._build_command(stamp)
        self.status.set("状态：正在启动 hmbatch 后台划分")
        with self.log_path.open("w", encoding="utf-8") as log:
            log.write("COMMAND: " + " ".join(cmd) + "\n\n")
        log_handle = self.log_path.open("a", encoding="utf-8", errors="replace")
        self.process = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        log_handle.close()
        self.status.set(f"状态：运行中，PID={self.process.pid}")
        self._append_log(f"日志文件：{self.log_path}\nPID={self.process.pid}\n\n")
        self.root.after(1000, self._poll)

    def stop(self) -> None:
        if not self.process or self.process.poll() is not None:
            self.status.set("状态：当前没有运行中的流程")
            return
        pid = self.process.pid
        self.status.set(f"状态：正在停止 PID={pid}")
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, timeout=30)
        except Exception as exc:
            self._append_log(f"\n停止命令异常：{exc}\n")
        self.status.set("状态：已发送停止命令")

    def _poll(self) -> None:
        if self.log_path and self.log_path.exists():
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
            if len(text) >= self.log_pos:
                new_text = text[self.log_pos :]
                self.log_pos = len(text)
                if new_text:
                    self._append_log(new_text)
            else:
                self.log_pos = 0
                self._replace_log(text)
        if self.process and self.process.poll() is None:
            self.root.after(1500, self._poll)
            return
        if self.process:
            code = self.process.poll()
            self.status.set("状态：划分完成" if code == 0 else f"状态：流程结束，返回码={code}")
            self._show_final_popup(code)
            self.process = None

    def _show_final_popup(self, return_code: int | None) -> None:
        stamp = self.current_stamp
        summary_path = RUNS_DIR / f"workflow_summary_{stamp}.json" if stamp else None
        if not summary_path or not summary_path.exists():
            log_tail = ""
            if self.log_path and self.log_path.exists():
                log_text = self.log_path.read_text(encoding="utf-8", errors="replace")
                interesting = [
                    line for line in log_text.splitlines()
                    if line.startswith("ERROR:") or "Could not find hmbatch.exe" in line
                ]
                log_tail = "\n".join(interesting[-5:]) or log_text[-800:]
            if return_code not in (0, None):
                message = "流程已经结束，但没有找到 summary JSON。"
                if log_tail:
                    message += f"\n\n关键日志：\n{log_tail}"
                message += "\n\n请查看下方日志和 runs 目录中的中文报告。"
                messagebox.showwarning("后台划分结束", message)
            return
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            messagebox.showwarning("后台划分结束", f"流程已经结束，但读取结果摘要失败：{exc}")
            return

        report_path = summary.get("report_path") or ""
        output_path = summary.get("output_hm_path") or ""
        errors = summary.get("errors") or []
        quality_errors = [item for item in errors if item.get("step") == "tetra_quality"]
        if quality_errors:
            lines = ["以下实体/批次触发 tetra 质量保护，最终可能保留 2D 面网格：", ""]
            for item in quality_errors:
                lines.append(f"- {item.get('status', 'unknown')}：{item.get('solid_count', 0)} 个")
                solids = item.get("solids") or []
                for solid in solids[:20]:
                    name = solid.get("component_name") or "未命名"
                    lines.append(f"  solid {solid.get('solid_id')}：{name}")
                if len(solids) > 20:
                    lines.append(f"  ... 还有 {len(solids) - 20} 个，详见中文报告")
            lines.extend(["", f"中文报告：{report_path}", f"输出模型：{output_path}"])
            messagebox.showwarning("后台划分质量提示", "\n".join(lines))
            return

        if summary.get("success"):
            messagebox.showinfo("后台划分完成", f"划分完成。\n\n输出模型：{output_path}\n中文报告：{report_path}")
        else:
            messagebox.showwarning("后台划分结束", f"流程结束，但存在失败或警告。\n\n中文报告：{report_path}\n日志文件：{self.log_path}")


def main() -> None:
    root = tk.Tk()
    BatchMeshingPanel(root)
    root.mainloop()


if __name__ == "__main__":
    main()
