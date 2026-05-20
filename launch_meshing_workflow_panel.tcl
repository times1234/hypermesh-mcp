# -*- coding: utf-8 -*-
#
# HyperMesh offline meshing workflow launcher.
#
# Usage in HyperMesh Tcl command window:
#   source "E:/mcp/hypermesh-mcp-server/launch_meshing_workflow_panel.tcl"

namespace eval ::hm_mcp_launcher {
    variable project_dir [file normalize [file dirname [info script]]]
    variable python_exe "python"
    variable host "127.0.0.1"
    variable port 47881
    variable listener_retry_count 10
    variable last_listener_errors ""
    variable output_path [file normalize [file join $project_dir outputs full_mesh_from_panel.hm]]
    variable auto_listener 1
    variable continue_on_error 1

    variable drag_element_size_min 0.5
    variable drag_element_size_max 1.5
    variable drag_fit_tolerance_ratio 0.05
    variable drag_retry_count 2
    variable drag_aspect_guard 1

    variable tetra_element_size_min 1.5
    variable tetra_element_size_max 2.0
    variable tetra_min_element_size_min 0.4
    variable tetra_min_element_size_max 0.6
    variable tetra_max_deviation 0.1
    variable tetra_feature_angle 30
    variable tetra_growth_rate 1.23
    variable tetra_fit_tolerance_ratio 0.01
    variable tetra_chord_dev_degrade_delta 0.20
    variable tetra_target_vol_skew 0.70
    variable tetra_repair_vol_skew 0.99
    variable use_gear_tooth_refinement 1
    variable gear_tooth_element_size_min 1.2
    variable gear_tooth_element_size_max 1.6
    variable gear_tooth_min_element_size_min 0.2
    variable gear_tooth_min_element_size_max 0.3
    variable gear_tooth_feature_angle 15
    variable spin_section_element_size_min 0.20
    variable spin_section_element_size_max 1.50
    variable spin_density_min 60
    variable spin_density_max 160

    variable probe_timeout 180
    variable phase2_timeout 120
    variable drag_timeout 900
    variable spin_timeout 900
    variable tetra_timeout 1800
    variable save_timeout 300

    variable current_pid ""
    variable current_log ""
    variable current_stamp ""
    variable last_log_size 0
    variable log_poll_ms 4000
    variable pid_check_ms 8000
    variable last_pid_check_ms 0
    variable log_max_lines 1200
    variable status_text "状态：未运行"
}

proc ::hm_mcp_launcher::last_nonempty_line {text} {
    set result ""
    foreach line [split $text "\n"] {
        set trimmed [string trim $line]
        if {$trimmed ne ""} {
            set result $trimmed
        }
    }
    return $result
}

proc ::hm_mcp_launcher::set_status {text} {
    variable status_text
    set status_text "状态：$text"
}

proc ::hm_mcp_launcher::panel_window {} {
    return .hm_mcp_meshing_launcher
}

proc ::hm_mcp_launcher::keep_panel_visible {} {
    variable current_pid
    set w [panel_window]
    if {![winfo exists $w]} {
        return
    }
    catch {
        wm attributes $w -topmost 1
    }
}

proc ::hm_mcp_launcher::append_log {text} {
    variable log_max_lines
    set widget .hm_mcp_meshing_launcher.root.logbox.text
    if {[winfo exists $widget]} {
        $widget configure -state normal
        $widget insert end $text
        set line_count [expr {int([$widget index end])}]
        if {$line_count > $log_max_lines} {
            set delete_to [expr {$line_count - $log_max_lines}]
            $widget delete 1.0 "$delete_to.0"
        }
        $widget see end
        $widget configure -state disabled
    }
}

proc ::hm_mcp_launcher::replace_log {text} {
    variable log_max_lines
    set widget .hm_mcp_meshing_launcher.root.logbox.text
    if {[winfo exists $widget]} {
        set lines [split $text "\n"]
        if {[llength $lines] > $log_max_lines} {
            set text [join [lrange $lines end-[expr {$log_max_lines - 1}] end] "\n"]
        }
        $widget configure -state normal
        $widget delete 1.0 end
        $widget insert end $text
        $widget see end
        $widget configure -state disabled
    }
}

proc ::hm_mcp_launcher::ensure_directories {} {
    variable project_dir
    file mkdir [file join $project_dir runs]
    file mkdir [file join $project_dir outputs]
}

proc ::hm_mcp_launcher::ensure_listener {} {
    variable project_dir
    variable python_exe
    variable port
    variable listener_retry_count
    variable last_listener_errors

    set old_pwd [pwd]
    set base_port $port
    set last_listener_errors ""
    set tried {}

    for {set offset 0} {$offset < $listener_retry_count} {incr offset} {
        set candidate [expr {$base_port + $offset}]
        if {[lsearch -exact $tried $candidate] >= 0} {
            continue
        }
        lappend tried $candidate
        set pycode [format {import hypermesh_mcp_server as hm; print(hm.create_gui_listener_tcl(port=%d)['script_path'])} $candidate]
        if {[catch {
            cd $project_dir
            set output [exec $python_exe -c $pycode]
        } err opts]} {
            append last_listener_errors "port=$candidate: create listener Tcl failed: $err\n"
            continue
        }

        set listener_path [last_nonempty_line $output]
        if {$listener_path eq ""} {
            append last_listener_errors "port=$candidate: Python 没有返回 listener Tcl 路径。\n"
            continue
        }
        set listener_path [file normalize $listener_path]
        if {![file exists $listener_path]} {
            append last_listener_errors "port=$candidate: listener Tcl 不存在：$listener_path\n"
            continue
        }
        if {[catch {source $listener_path} source_err source_opts]} {
            append last_listener_errors "port=$candidate: source listener failed: $source_err\n"
            continue
        }
        catch {cd $old_pwd}
        set port $candidate
        return $listener_path
    }

    catch {cd $old_pwd}
    error "所有自动连接端口都失败，已尝试：$tried\n$last_listener_errors"
}

proc ::hm_mcp_launcher::manual_connection_help_text {} {
    variable project_dir
    variable port
    variable listener_retry_count
    variable last_listener_errors
    set backup_port [expr {$port + $listener_retry_count}]
    set panel_path [file normalize [info script]]
    set ps_dir [file nativename $project_dir]
    set panel_tcl [string map {"\\" "/"} $panel_path]
    set py_cmd [format {python -c "import hypermesh_mcp_server as hm; print(hm.create_gui_listener_tcl(port=%d)['script_path'])"} $backup_port]
    return "\n手动建立连接方法：\n\n自动连接已经尝试从当前端口开始的一组备用端口。如果仍失败，建议不要继续使用当前端口 $port，先换新的备用端口 $backup_port。\n\n1. 在 PowerShell 中运行：\n   cd \"$ps_dir\"\n   $py_cmd\n\n2. 复制 PowerShell 输出的 Tcl 文件路径。\n\n3. 在 HyperMesh 的 Tcl 命令窗口中运行，注意把路径替换成上一步实际输出的路径：\n   source \"E:/mcp/hypermesh-mcp-server/runs/hypermesh_mcp_xxxxxx.tcl\"\n\n4. 告诉本面板后续使用同一个备用端口，在 HyperMesh Tcl 命令窗口中运行：\n   set ::hm_mcp_launcher::port $backup_port\n\n5. 如果只是重新打开本面板，也可以在 HyperMesh 中运行：\n   source \"$panel_tcl\"\n\n6. 连接成功后，回到本面板取消勾选“开始前自动建立/刷新 HyperMesh 连接”，再点击“开始划分”。\n\n自动连接失败详情：\n$last_listener_errors\n"
}

proc ::hm_mcp_launcher::make_stamp {} {
    return [clock format [clock seconds] -format "%Y%m%d_%H%M%S"]
}

proc ::hm_mcp_launcher::clamp_number {value low high fallback} {
    if {[catch {set number [expr {double($value)}]}]} {
        return $fallback
    }
    if {$number < $low} {
        return $low
    }
    if {$number > $high} {
        return $high
    }
    return $number
}

proc ::hm_mcp_launcher::normalize_pair {min_var max_var fallback_min fallback_max} {
    upvar 1 $min_var min_value
    upvar 1 $max_var max_value
    set min_value [clamp_number $min_value 0.000001 1000000 $fallback_min]
    set max_value [clamp_number $max_value 0.000001 1000000 $fallback_max]
    if {$min_value > $max_value} {
        set tmp $min_value
        set min_value $max_value
        set max_value $tmp
    }
}

proc ::hm_mcp_launcher::normalize_mesh_parameters {} {
    variable drag_element_size_min
    variable drag_element_size_max
    variable tetra_element_size_min
    variable tetra_element_size_max
    variable tetra_min_element_size_min
    variable tetra_min_element_size_max
    variable gear_tooth_element_size_min
    variable gear_tooth_element_size_max
    variable gear_tooth_min_element_size_min
    variable gear_tooth_min_element_size_max
    variable spin_section_element_size_min
    variable spin_section_element_size_max
    variable spin_density_min
    variable spin_density_max

    normalize_pair drag_element_size_min drag_element_size_max 0.5 1.5
    normalize_pair tetra_element_size_min tetra_element_size_max 1.5 2.0
    normalize_pair tetra_min_element_size_min tetra_min_element_size_max 0.4 0.6
    normalize_pair gear_tooth_element_size_min gear_tooth_element_size_max 1.2 1.6
    normalize_pair gear_tooth_min_element_size_min gear_tooth_min_element_size_max 0.2 0.3
    normalize_pair spin_section_element_size_min spin_section_element_size_max 0.20 1.50
    normalize_pair spin_density_min spin_density_max 60 160
    set spin_density_min [expr {int(round($spin_density_min))}]
    set spin_density_max [expr {int(round($spin_density_max))}]
    append_log "尺寸限制：drag=$drag_element_size_min..$drag_element_size_max, tetra目标=$tetra_element_size_min..$tetra_element_size_max, tetra最小=$tetra_min_element_size_min..$tetra_min_element_size_max, spin截面=$spin_section_element_size_min..$spin_section_element_size_max, spin份数=$spin_density_min..$spin_density_max\n"
}

proc ::hm_mcp_launcher::build_command {{mode full}} {
    variable project_dir
    variable python_exe
    variable host
    variable port
    variable output_path
    variable continue_on_error
    variable drag_element_size_min
    variable drag_element_size_max
    variable drag_fit_tolerance_ratio
    variable drag_retry_count
    variable drag_aspect_guard
    variable tetra_element_size_min
    variable tetra_element_size_max
    variable tetra_min_element_size_min
    variable tetra_min_element_size_max
    variable tetra_max_deviation
    variable tetra_feature_angle
    variable tetra_growth_rate
    variable tetra_fit_tolerance_ratio
    variable tetra_chord_dev_degrade_delta
    variable tetra_target_vol_skew
    variable tetra_repair_vol_skew
    variable use_gear_tooth_refinement
    variable gear_tooth_element_size_min
    variable gear_tooth_element_size_max
    variable gear_tooth_min_element_size_min
    variable gear_tooth_min_element_size_max
    variable gear_tooth_feature_angle
    variable spin_section_element_size_min
    variable spin_section_element_size_max
    variable spin_density_min
    variable spin_density_max
    variable probe_timeout
    variable phase2_timeout
    variable drag_timeout
    variable spin_timeout
    variable tetra_timeout
    variable save_timeout
    variable current_stamp

    set runner [file join $project_dir run_full_meshing_workflow.py]
    set cmd [list $python_exe $runner \
        --host $host \
        --port $port \
        --output $output_path \
        --stamp $current_stamp \
        --probe-timeout $probe_timeout \
        --phase2-timeout $phase2_timeout \
        --drag-timeout $drag_timeout \
        --spin-timeout $spin_timeout \
        --tetra-timeout $tetra_timeout \
        --save-timeout $save_timeout \
        --drag-element-size $drag_element_size_max \
        --drag-element-size-min $drag_element_size_min \
        --drag-element-size-max $drag_element_size_max \
        --drag-fit-tolerance-ratio $drag_fit_tolerance_ratio \
        --drag-retry-count $drag_retry_count \
        --spin-section-element-size-min $spin_section_element_size_min \
        --spin-section-element-size-max $spin_section_element_size_max \
        --spin-density-min $spin_density_min \
        --spin-density-max $spin_density_max \
        --tetra-element-size $tetra_element_size_max \
        --tetra-element-size-min $tetra_element_size_min \
        --tetra-element-size-max $tetra_element_size_max \
        --tetra-min-element-size $tetra_min_element_size_max \
        --tetra-min-element-size-min $tetra_min_element_size_min \
        --tetra-min-element-size-max $tetra_min_element_size_max \
        --tetra-max-deviation $tetra_max_deviation \
        --tetra-feature-angle $tetra_feature_angle \
        --tetra-growth-rate $tetra_growth_rate \
        --tetra-fit-tolerance-ratio $tetra_fit_tolerance_ratio \
        --tetra-chord-dev-degrade-delta $tetra_chord_dev_degrade_delta \
        --tetra-target-vol-skew $tetra_target_vol_skew \
        --tetra-repair-vol-skew $tetra_repair_vol_skew \
        --gear-tooth-element-size $gear_tooth_element_size_max \
        --gear-tooth-element-size-min $gear_tooth_element_size_min \
        --gear-tooth-element-size-max $gear_tooth_element_size_max \
        --gear-tooth-min-element-size $gear_tooth_min_element_size_max \
        --gear-tooth-min-element-size-min $gear_tooth_min_element_size_min \
        --gear-tooth-min-element-size-max $gear_tooth_min_element_size_max \
        --gear-tooth-feature-angle $gear_tooth_feature_angle]
    if {$continue_on_error} {
        lappend cmd --continue-on-error
    } else {
        lappend cmd --stop-on-error
    }
    if {$drag_aspect_guard} {
        lappend cmd --drag-aspect-guard
    }
    if {$use_gear_tooth_refinement} {
        lappend cmd --use-gear-tooth-refinement
    } else {
        lappend cmd --no-gear-tooth-refinement
    }
    if {$mode eq "gear_preview"} {
        lappend cmd --gear-tooth-preview-only
    } elseif {$mode eq "delete_gear_preview"} {
        lappend cmd --delete-gear-tooth-preview
    }
    return $cmd
}

proc ::hm_mcp_launcher::pid_running {pid} {
    if {$pid eq ""} {
        return 0
    }
    if {[catch {set out [exec tasklist /FI "PID eq $pid" /NH]}]} {
        return 0
    }
    if {[string first $pid $out] >= 0} {
        return 1
    }
    return 0
}

proc ::hm_mcp_launcher::read_new_log_data {} {
    variable current_log
    variable last_log_size

    if {$current_log eq "" || ![file exists $current_log]} {
        return ""
    }

    set size [file size $current_log]
    if {$size < $last_log_size} {
        set last_log_size 0
        replace_log ""
    }
    if {$size <= $last_log_size} {
        return ""
    }

    if {[catch {
        set fh [open $current_log r]
        fconfigure $fh -encoding utf-8
        seek $fh $last_log_size start
        set text [read $fh [expr {$size - $last_log_size}]]
        close $fh
    } read_err]} {
        catch {close $fh}
        return ""
    }
    set last_log_size $size
    return $text
}

proc ::hm_mcp_launcher::poll_log {} {
    variable current_pid
    variable current_log
    variable log_poll_ms
    variable pid_check_ms
    variable last_pid_check_ms

    set new_text [read_new_log_data]
    if {$new_text ne ""} {
        append_log $new_text
    }

    if {$current_pid ne ""} {
        set now_ms [clock milliseconds]
        if {$last_pid_check_ms == 0 || ($now_ms - $last_pid_check_ms) >= $pid_check_ms} {
            set last_pid_check_ms $now_ms
            set still_running [pid_running $current_pid]
        } else {
            set still_running 1
        }
        if {$still_running} {
            after $log_poll_ms ::hm_mcp_launcher::poll_log
        } else {
            set current_pid ""
            keep_panel_visible
            if {$current_log ne "" && [file exists $current_log]} {
                set final_text [read_new_log_data]
                if {$final_text ne ""} {
                    append_log $final_text
                }
                set fh [open $current_log r]
                fconfigure $fh -encoding utf-8
                set text [read $fh]
                close $fh
                if {[string first "Success: True" $text] >= 0} {
                    set_status "完成划分"
                } else {
                    set_status "流程结束，但存在失败或警告，请查看日志"
                }
            } else {
                set_status "流程结束，但未找到日志"
            }
        }
    }
}

proc ::hm_mcp_launcher::start_workflow {{mode full}} {
    variable project_dir
    variable auto_listener
    variable port
    variable current_pid
    variable current_log
    variable current_stamp
    variable last_log_size
    variable last_pid_check_ms

    if {$current_pid ne ""} {
        set_status "当前已有流程在运行"
        append_log "\n当前已有后台流程在运行，先停止或等待它结束。\n"
        return
    }

    ensure_directories
    set current_stamp [make_stamp]
    if {$mode eq "gear_preview"} {
        set current_log [file normalize [file join $project_dir runs "panel_gear_tooth_preview_$current_stamp.log"]]
    } elseif {$mode eq "delete_gear_preview"} {
        set current_log [file normalize [file join $project_dir runs "panel_delete_gear_tooth_preview_$current_stamp.log"]]
    } else {
        set current_log [file normalize [file join $project_dir runs "panel_workflow_$current_stamp.log"]]
    }
    set last_log_size 0
    set last_pid_check_ms 0
    replace_log ""
    normalize_mesh_parameters

    if {$auto_listener} {
        set_status "正在建立 HyperMesh 连接"
        append_log "正在建立 HyperMesh 连接...\n"
        if {[catch {set listener_path [ensure_listener]} err]} {
            set_status "连接失败"
            append_log "自动建立连接失败：$err\n"
            append_log [manual_connection_help_text]
            return
        }
        append_log "连接成功：$listener_path\n当前端口：$port\n"
    }

    set cmd [build_command $mode]
    set old_pwd [pwd]
    set_status "正在启动后台划分流程"
    append_log "正在启动后台划分流程...\n日志文件：$current_log\n\n"
    if {[catch {
        cd $project_dir
        set pids [exec {*}$cmd > $current_log 2>@1 &]
    } err opts]} {
        catch {cd $old_pwd}
        set_status "启动失败"
        append_log "无法启动离线划分流程：$err\n"
        return
    }
    catch {cd $old_pwd}

    set current_pid [lindex $pids 0]
    set_status "运行中，PID=$current_pid"
    append_log "已开始运行，PID=$current_pid。\n\n"
    keep_panel_visible
    after 1000 ::hm_mcp_launcher::poll_log
}

proc ::hm_mcp_launcher::stop_workflow {} {
    variable current_pid
    variable current_log

    if {$current_pid eq ""} {
        set_status "当前没有运行中的流程"
        append_log "\n当前没有由这个面板启动的后台 Python 流程。\n"
        keep_panel_visible
        return
    }

    set pid $current_pid
    set current_pid ""
    set_status "正在停止 PID=$pid"
    append_log "\n正在停止后台 Python 流程 PID=$pid ...\n"
    if {[catch {exec taskkill /PID $pid /T /F} err]} {
        set_status "停止命令已执行，请检查日志"
        append_log "停止命令返回：$err\n如果 HyperMesh 已经开始执行一段很长的 Tcl，可能会完成当前命令后才停下来。\n日志：$current_log\n"
    } else {
        set_status "已停止后台 Python 流程"
        append_log "后台 Python 流程已停止。如果 HyperMesh 正在执行已提交的 Tcl，可能还会短暂继续当前命令。\n"
    }
    keep_panel_visible
}

proc ::hm_mcp_launcher::browse_output_path {} {
    variable output_path
    set picked [tk_getSaveFile -title "选择最终 HM 文件" -defaultextension ".hm" -filetypes {{"HyperMesh files" {.hm}} {"All files" {*}}}]
    if {$picked ne ""} {
        set output_path [file normalize $picked]
    }
}

proc ::hm_mcp_launcher::add_row {parent row label variable hint} {
    ttk::label $parent.l$row -text $label -anchor w -style HMLabel.TLabel
    ttk::entry $parent.e$row -textvariable $variable -width 16 -style HM.TEntry
    grid $parent.l$row -row $row -column 0 -sticky ew -padx {0 10} -pady 4
    grid $parent.e$row -row $row -column 1 -sticky ew -pady 4
    if {$hint ne ""} {
        ttk::label $parent.h$row -text $hint -anchor w -style HMHunt.TLabel
        grid $parent.h$row -row $row -column 2 -sticky ew -padx {8 0} -pady 4
    }
}

proc ::hm_mcp_launcher::build_ui {} {
    if {[catch {package require Tk} err]} {
        puts "无法加载 Tk，不能显示弹窗：$err"
        return
    }

    catch {package require Ttk}
    catch {
        set current_theme [ttk::style theme use]
        if {![regexp {^hw} $current_theme]} {
            foreach candidate [ttk::style theme names] {
                if {[regexp {^hw} $candidate]} {
                    ttk::style theme use $candidate
                    break
                }
            }
        }
    }
    set w .hm_mcp_meshing_launcher
    if {[winfo exists $w]} {
        destroy $w
    }

    toplevel $w
    wm title $w "HyperMesh 自动网格划分"
    wm minsize $w 980 680
    wm geometry $w 1080x1260+80+40
    catch {wm attributes $w -topmost 1}

    catch {
        ttk::style configure HMRoot.TFrame -background "#edf1f7"
        ttk::style configure HMCard.TFrame -background "#ffffff" -relief flat
        ttk::style configure HMTitle.TLabel -background "#edf1f7" -foreground "#172033" -font {"Microsoft YaHei UI" 16 bold}
        ttk::style configure HMSub.TLabel -background "#edf1f7" -foreground "#5c6675" -font {"Microsoft YaHei UI" 10}
        ttk::style configure HMSection.TLabelframe -background "#ffffff" -foreground "#172033" -font {"Microsoft YaHei UI" 13 bold}
        ttk::style configure HMSection.TLabelframe.Label -background "#ffffff" -foreground "#172033" -font {"Microsoft YaHei UI" 13 bold}
        ttk::style configure HMLabel.TLabel -background "#ffffff" -foreground "#1e293b" -font {"Microsoft YaHei UI" 12}
        ttk::style configure HMHunt.TLabel -background "#ffffff" -foreground "#697386" -font {"Microsoft YaHei UI" 9}
        ttk::style configure HMStatus.TLabel -background "#dbeafe" -foreground "#17324d" -font {"Microsoft YaHei UI" 12 bold} -padding {10 6}
        ttk::style configure HM.TEntry -font {"Microsoft YaHei UI" 12} -padding 5
        ttk::style configure HM.TButton -font {"Microsoft YaHei UI" 12 bold} -padding {14 7}
        ttk::style configure HMStop.TButton -font {"Microsoft YaHei UI" 12 bold} -padding {14 7}
        ttk::style configure HMCheck.TCheckbutton -background "#ffffff" -foreground "#1e293b" -font {"Microsoft YaHei UI" 11}
    }

    ttk::frame $w.root -padding 16 -style HMRoot.TFrame
    grid $w.root -row 0 -column 0 -sticky nsew
    grid rowconfigure $w 0 -weight 1
    grid columnconfigure $w 0 -weight 1
    grid columnconfigure $w.root 0 -weight 1
    grid rowconfigure $w.root 5 -weight 1

    ttk::label $w.root.title -text "HyperMesh 自动网格划分面板" -style HMTitle.TLabel
    ttk::label $w.root.sub -text "导入模型后，点击开始划分即可执行完整流程；运行日志会实时显示在下方。" -style HMSub.TLabel
    grid $w.root.title -row 0 -column 0 -sticky ew
    grid $w.root.sub -row 1 -column 0 -sticky ew -pady {2 8}

    ttk::label $w.root.status -textvariable ::hm_mcp_launcher::status_text -style HMStatus.TLabel -anchor w
    grid $w.root.status -row 2 -column 0 -sticky ew -pady {0 10}

    ttk::frame $w.root.main -style HMRoot.TFrame
    grid $w.root.main -row 3 -column 0 -sticky ew -pady {0 10}
    grid columnconfigure $w.root.main 0 -weight 1
    grid columnconfigure $w.root.main 1 -weight 1

    ttk::labelframe $w.root.main.left -text "项目和尺寸限制" -padding 12 -style HMSection.TLabelframe
    grid $w.root.main.left -row 0 -column 0 -sticky nsew -padx {0 8}
    grid columnconfigure $w.root.main.left 1 -weight 1
    add_row $w.root.main.left 0 "项目目录" ::hm_mcp_launcher::project_dir ""
    ttk::label $w.root.main.left.l1 -text "输出 HM 文件" -anchor w -style HMLabel.TLabel
    ttk::entry $w.root.main.left.e1 -textvariable ::hm_mcp_launcher::output_path -style HM.TEntry
    ttk::button $w.root.main.left.b1 -text "浏览" -style HM.TButton -command ::hm_mcp_launcher::browse_output_path
    grid $w.root.main.left.l1 -row 1 -column 0 -sticky ew -padx {0 12} -pady 5
    grid $w.root.main.left.e1 -row 1 -column 1 -sticky ew -pady 5
    grid $w.root.main.left.b1 -row 1 -column 2 -sticky ew -padx {8 0} -pady 5
    ttk::separator $w.root.main.left.sep1 -orient horizontal
    grid $w.root.main.left.sep1 -row 2 -column 0 -columnspan 3 -sticky ew -pady 7
    add_row $w.root.main.left 3 "drag 尺寸下限" ::hm_mcp_launcher::drag_element_size_min "自动尺寸下限"
    add_row $w.root.main.left 4 "drag 尺寸上限" ::hm_mcp_launcher::drag_element_size_max "自动尺寸上限"
    add_row $w.root.main.left 5 "tetra目标下限" ::hm_mcp_launcher::tetra_element_size_min "目标尺寸下限"
    add_row $w.root.main.left 6 "tetra目标上限" ::hm_mcp_launcher::tetra_element_size_max "目标尺寸上限"
    add_row $w.root.main.left 7 "tetra最小下限" ::hm_mcp_launcher::tetra_min_element_size_min "最小尺寸下限"
    add_row $w.root.main.left 8 "tetra最小上限" ::hm_mcp_launcher::tetra_min_element_size_max "最小尺寸上限"
    add_row $w.root.main.left 9 "spin份数下限" ::hm_mcp_launcher::spin_density_min "按半径自动计算后的下限"
    add_row $w.root.main.left 10 "spin份数上限" ::hm_mcp_launcher::spin_density_max "按半径自动计算后的上限"
    ttk::checkbutton $w.root.main.left.auto -text "开始前自动建立/刷新 HyperMesh 连接" -variable ::hm_mcp_launcher::auto_listener -style HMCheck.TCheckbutton
    add_row $w.root.main.left 11 "spin section size min" ::hm_mcp_launcher::spin_section_element_size_min "spin section 2D size min"
    add_row $w.root.main.left 12 "spin section size max" ::hm_mcp_launcher::spin_section_element_size_max "spin section 2D size max"
    grid $w.root.main.left.auto -row 13 -column 0 -columnspan 3 -sticky w -pady {7 0}

    ttk::labelframe $w.root.main.right -text "质量和修复参数" -padding 12 -style HMSection.TLabelframe
    grid $w.root.main.right -row 0 -column 1 -sticky nsew -padx {8 0}
    grid columnconfigure $w.root.main.right 1 -weight 0 -minsize 92
    grid columnconfigure $w.root.main.right 2 -weight 1
    ttk::checkbutton $w.root.main.right.dragaspect -text "drag三层" -variable ::hm_mcp_launcher::drag_aspect_guard -style HMCheck.TCheckbutton
    grid $w.root.main.right.dragaspect -row 9 -column 0 -columnspan 3 -sticky w -pady {7 0}
    add_row $w.root.main.right 0 "drag 贴合比例" ::hm_mcp_launcher::drag_fit_tolerance_ratio "越小越严格"
    add_row $w.root.main.right 1 "drag 重试次数" ::hm_mcp_launcher::drag_retry_count ""
    add_row $w.root.main.right 2 "tetra 最大偏差" ::hm_mcp_launcher::tetra_max_deviation "用于 HyperMesh 生成 2D 面网格的 max_dev"
    add_row $w.root.main.right 3 "tetra 特征角" ::hm_mcp_launcher::tetra_feature_angle ""
    add_row $w.root.main.right 4 "tetra 增长率" ::hm_mcp_launcher::tetra_growth_rate ""
    add_row $w.root.main.right 5 "tetra 贴合比例" ::hm_mcp_launcher::tetra_fit_tolerance_ratio ""
    add_row $w.root.main.right 6 "最大 chord dev 下降值" ::hm_mcp_launcher::tetra_chord_dev_degrade_delta "修复后最大 chord dev 增量超过此值则退回"
    add_row $w.root.main.right 7 "目标 vol skew" ::hm_mcp_launcher::tetra_target_vol_skew ""
    add_row $w.root.main.right 8 "修复 vol skew" ::hm_mcp_launcher::tetra_repair_vol_skew ""
    ttk::checkbutton $w.root.main.right.gearrefine -text "启用齿面加厚/加密模型" -variable ::hm_mcp_launcher::use_gear_tooth_refinement -style HMCheck.TCheckbutton
    grid $w.root.main.right.gearrefine -row 10 -column 0 -columnspan 3 -sticky w -pady {7 0}
    add_row $w.root.main.right 11 "齿面tetra下限" ::hm_mcp_launcher::gear_tooth_element_size_min "默认=上限，固定尺寸"
    add_row $w.root.main.right 12 "齿面tetra上限" ::hm_mcp_launcher::gear_tooth_element_size_max "默认=下限，固定尺寸"
    add_row $w.root.main.right 13 "齿面最小下限" ::hm_mcp_launcher::gear_tooth_min_element_size_min "默认=上限，固定最小尺寸"
    add_row $w.root.main.right 14 "齿面最小上限" ::hm_mcp_launcher::gear_tooth_min_element_size_max "默认=下限，固定最小尺寸"
    add_row $w.root.main.right 15 "齿面特征角" ::hm_mcp_launcher::gear_tooth_feature_angle "默认比普通tetra小30%"

    ttk::labelframe $w.root.logbox -text "运行日志" -padding 12 -style HMSection.TLabelframe
    grid $w.root.logbox -row 5 -column 0 -sticky nsew -pady {0 10}
    grid rowconfigure $w.root.logbox 0 -weight 1
    grid columnconfigure $w.root.logbox 0 -weight 1
    text $w.root.logbox.text -height 8 -wrap none -state disabled -font {"Consolas" 11} -background "#101828" -foreground "#e5e7eb" -insertbackground "#e5e7eb" -relief flat -borderwidth 0
    ttk::scrollbar $w.root.logbox.ys -orient vertical -command "$w.root.logbox.text yview"
    ttk::scrollbar $w.root.logbox.xs -orient horizontal -command "$w.root.logbox.text xview"
    $w.root.logbox.text configure -yscrollcommand "$w.root.logbox.ys set" -xscrollcommand "$w.root.logbox.xs set"
    grid $w.root.logbox.text -row 0 -column 0 -sticky nsew
    grid $w.root.logbox.ys -row 0 -column 1 -sticky ns
    grid $w.root.logbox.xs -row 1 -column 0 -sticky ew

    ttk::frame $w.root.actions -style HMRoot.TFrame
    grid $w.root.actions -row 6 -column 0 -sticky ew
    ttk::button $w.root.actions.connect -text "仅建立连接" -style HM.TButton -command {
        ::hm_mcp_launcher::set_status "正在建立 HyperMesh 连接"
        ::hm_mcp_launcher::append_log "正在建立 HyperMesh 连接...\n"
        if {[catch {set p [::hm_mcp_launcher::ensure_listener]} e]} {
            ::hm_mcp_launcher::set_status "连接失败"
            ::hm_mcp_launcher::append_log "连接失败：$e\n"
            ::hm_mcp_launcher::append_log [::hm_mcp_launcher::manual_connection_help_text]
        } else {
            ::hm_mcp_launcher::set_status "连接成功"
            ::hm_mcp_launcher::append_log "连接成功：$p\n当前端口：$::hm_mcp_launcher::port\n"
        }
    }
    ttk::button $w.root.actions.run -text "开始划分" -style HM.TButton -command ::hm_mcp_launcher::start_workflow
    ttk::button $w.root.actions.gearpreview -text "只划分齿面网格" -style HM.TButton -command {::hm_mcp_launcher::start_workflow gear_preview}
    ttk::button $w.root.actions.deletegearpreview -text "删除齿面网格" -style HM.TButton -command {::hm_mcp_launcher::start_workflow delete_gear_preview}
    ttk::button $w.root.actions.stop -text "停止当前流程" -style HMStop.TButton -command ::hm_mcp_launcher::stop_workflow
    grid $w.root.actions.connect -row 0 -column 0 -padx {0 12}
    grid $w.root.actions.run -row 0 -column 1 -padx 12
    grid $w.root.actions.gearpreview -row 0 -column 2 -padx 12
    grid $w.root.actions.deletegearpreview -row 0 -column 3 -padx 12
    grid $w.root.actions.stop -row 0 -column 4 -padx 12
}

::hm_mcp_launcher::build_ui
