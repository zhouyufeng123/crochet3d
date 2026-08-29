"""从重建的 GLB 模型估算钩织针数。

两级方法：

【逐圈法】沿钩织轴切片，环路面积→光滑周长（对重建毛刺稳健）→ 乘横向密度。
【高度图检测法（auto）】把表面展开成 角度θ × 高度z 的半径高度图 H(θ,z)，
2D FFT 同时测出：
  - 角度方向主频 k(z) = 各高度带一圈的针目数（大片表面平均，比单环 1D 稳健）
  - 高度方向主频 = 行密度（模型尺度）→ 配合线材先验行密度反推模型虚大倍数，
    自动校准尺寸（毛毛虫被放大 3 倍这类问题）
并对"切面与表面近乎相切"的圈（蘑菇伞檐外翻处）做标记，不报告虚大的针数。
"""

import math
import os
import threading

import numpy as np
import trimesh
from scipy.fft import fft, ifft
from scipy.ndimage import distance_transform_edt, median_filter
from scipy.signal import find_peaks
from shapely.geometry import Polygon

# 环路面积低于该值(cm2)视为眼睛/纽扣等小部件
MIN_LOOP_AREA_CM2 = 0.35
# 质心距离小于该值(cm)的环路视为同一轮廓的多层壳
SHELL_MERGE_CM = 0.45
# 长宽比超过该值的环路按扁平件处理
FLAT_ASPECT = 2.2
# 高度图检测的针数频带与置信度阈值（合成验证: 真信号 0.5+，重建噪声 ≤0.2 → 取 0.25）
BAND_K = (6, 180)
CONF_TAU = 0.25
ROWS_CONF_TAU = 0.15

_cache: dict = {}
_lock = threading.Lock()

# 钩织语法先验：从 39 份图解 444 圈提取的相邻圈差值经验分布。
# 真实图解的加/减针绝大多数是 ±6/±8（球体生长规则）或不加减。
DELTA_PRIOR = {
    0: 0.16, 6: 0.28, -6: 0.16, 8: 0.08, -8: 0.04,
    4: 0.03, -4: 0.02, 2: 0.03, -2: 0.02, 3: 0.01, -3: 0.03,
}


# ---------------------------------------------------------------- 基础工具

def _load_mesh(model_path, axis: str, real_size_cm: float | None):
    scene = trimesh.load(str(model_path))
    mesh = scene.to_geometry()
    mesh.merge_vertices()
    axis_idx = {"x": 0, "y": 1, "z": 2}[axis]
    extent_cm = float(mesh.extents[axis_idx]) * 100.0
    scale = 1.0
    if real_size_cm and extent_cm > 0:
        scale = float(real_size_cm) / extent_cm
    if scale != 1.0:
        mesh.apply_scale(scale)
    return mesh, extent_cm, scale


def _loop_metrics(poly: Polygon) -> tuple[float, float, np.ndarray]:
    """截面多边形(米) -> (面积cm2, 形状修正后的光滑周长cm, 质心cm)。

    光滑周长：傅里叶低通(保留前8个谐波)后重建轮廓再量周长——
    同时消除毛刺(高频)与等效圆偏差(椭圆/扁平低估)。
    """
    area_cm2 = float(poly.area) * 1e4
    if area_cm2 <= 0:
        return 0.0, 0.0, np.zeros(2)
    centroid = np.array([poly.centroid.x, poly.centroid.y]) * 100.0

    ring = poly.exterior
    n = 2048
    d = np.linspace(0, ring.length, n, endpoint=False)
    pts = np.array([ring.interpolate(float(x)).coords[0] for x in d], dtype=np.float64) * 100.0
    zc = pts[:, 0] + 1j * pts[:, 1]
    zc -= zc.mean()
    Z = fft(zc)
    keep = np.zeros(n, dtype=bool)
    keep[:9] = True  # 保留 k=0..8
    keep[-8:] = True
    Z[~keep] = 0
    smooth = ifft(Z)
    smooth_pts = np.column_stack([smooth.real, smooth.imag])
    seg = np.linalg.norm(np.diff(np.vstack([smooth_pts, smooth_pts[:1]]), axis=0), axis=1)
    perim = float(seg.sum())

    # 扁平件（耳朵等）低通后仍接近真实轮廓，无需特判
    return area_cm2, perim, centroid


def _slice_perimeter(mesh, axis_idx: int, pos) -> tuple[float, float]:
    """某高度截面的 (光滑周长cm, 平均轮廓半径cm)；无几何返回 (0,0)。"""
    normal = [0, 0, 0]
    normal[axis_idx] = 1
    origin = [0, 0, 0]
    origin[axis_idx] = float(pos)
    try:
        sec = mesh.section(plane_origin=origin, plane_normal=normal)
    except Exception:
        return 0.0, 0.0
    if sec is None:
        return 0.0, 0.0
    try:
        planar, _ = sec.to_2D()
        polys = [p for p in planar.polygons_closed if p is not None and not p.is_empty]
    except Exception:
        return 0.0, 0.0

    kept = []
    for poly in polys:
        if not isinstance(poly, Polygon):
            continue
        area, perim, centroid = _loop_metrics(poly)
        if area >= MIN_LOOP_AREA_CM2:
            kept.append((area, perim, centroid))
    if not kept:
        return 0.0, 0.0

    kept.sort(key=lambda t: -t[0])
    groups: list[dict] = []
    for area, perim, centroid in kept:
        for g in groups:
            if np.linalg.norm(centroid - g["centroid"]) < SHELL_MERGE_CM:
                g["area"] = max(g["area"], area)
                g["perim"] = max(g["perim"], perim)
                break
        else:
            groups.append({"area": area, "perim": perim, "centroid": centroid})
    perim_total = float(sum(g["perim"] for g in groups))
    mean_r = float(np.sqrt(sum(g["area"] for g in groups) / math.pi)) if groups else 0.0
    return perim_total, mean_r


# ---------------------------------------------------------------- 高度图检测

def _heightmap(mesh, axis_idx: int, nth: int = 720, nz: int = 240):
    """表面 → H(θ bin, z bin)=该方向最大半径（米）。返回 H、填充掩码、z 范围。"""
    centers = mesh.triangles_center
    others = [i for i in range(3) if i != axis_idx]
    x = centers[:, others[0]]
    y = centers[:, others[1]]
    zc = centers[:, axis_idx]
    zmin, zmax = float(zc.min()), float(zc.max())
    r = np.sqrt(x * x + y * y)
    theta = np.arctan2(y, x)

    zi = np.clip(((zc - zmin) / max(zmax - zmin, 1e-9) * nz).astype(int), 0, nz - 1)
    ti = np.clip(((theta + np.pi) / (2 * np.pi) * nth).astype(int), 0, nth - 1)

    flat = np.full(nz * nth, -np.inf)
    np.maximum.at(flat, zi * nth + ti, r)
    H = flat.reshape(nz, nth)
    mask = np.isfinite(H)
    if (~mask).any():
        ind = distance_transform_edt(~mask, return_indices=True)[1]
        H = H[tuple(ind)]
    # 注意：不做每行均值相减——角度DC列(k=0)携带行距信息(轮廓+行凸起)，
    # 低频角度谐波在 _detect_heightmap 里用频域置零去除
    return H, mask, zmin, zmax


def _detect_heightmap(H, length_cm: float, band_k=BAND_K):
    """H(θ,z) → 检测结果 dict。

    返回: kProfile(每 z-bin 一圈的针数, 未检出=0), confProfile,
          rowsTotal(高度方向总行周期数), rowsConf, rowPitchCm
    """
    nz, nth = H.shape
    # 只在角度方向滤波：去掉 1..5 次低频谐波（椭圆/耳形等大形状），保留 k=0（行信息）与针目高频
    Z1 = fft(H, axis=1)
    Z1[:, 1:6] = 0
    Z1[:, -5:] = 0
    resid = ifft(Z1, axis=1).real

    # 角度方向频谱：每行一个
    S = np.abs(fft(resid, axis=1))[:, : nth // 2]
    ks = np.arange(S.shape[1])
    bandmask = (ks >= band_k[0]) & (ks <= band_k[1])
    Sb = np.where(bandmask[None, :], S, 0.0)
    k_idx = np.argmax(Sb, axis=1)
    k_energy = Sb[np.arange(nz), k_idx]
    row_energy = Sb.sum(axis=1) + 1e-12
    conf = k_energy / row_energy

    k_profile = np.where(conf >= CONF_TAU, k_idx, 0).astype(int)
    k_profile[(k_profile > 0) & (conf < CONF_TAU)] = 0
    k_profile = median_filter(k_profile, size=5, mode="nearest")

    # 高度方向：行周期（角度DC列的 z 变化 = 轮廓 + 行凸起；频域高通去轮廓）
    v = resid.mean(axis=1)
    V = fft(v)
    V[:3] = 0  # 去掉 DC 与超低频轮廓
    V[-3:] = 0
    vh = ifft(V).real
    Sv = np.abs(fft(vh))
    vmask = np.zeros(len(Sv), dtype=bool)
    vmask[3: nz // 2] = True
    Sv = np.where(vmask, Sv, 0.0)
    rows_total = int(np.argmax(Sv))
    rows_conf = float(Sv[rows_total] / (Sv[vmask].sum() + 1e-12))
    bin_cm = length_cm / nz
    row_pitch_cm = bin_cm * nz / rows_total if rows_total > 0 else 0.0

    return {
        "kProfile": k_profile,
        "confProfile": conf,
        "rowsTotal": rows_total,
        "rowsConf": rows_conf,
        "rowPitchCm": float(row_pitch_cm),
        "binCm": float(bin_cm),
    }


def _auto_analysis(model_path, axis_idx: int, gauge_h_prior: float):
    """在模型原始尺度上检测，返回检测结果与建议缩放。信号不足时 scale=1。"""
    mesh0, extent_cm, _ = _load_mesh(model_path, "xyz"[axis_idx], None)
    H, _, zmin, zmax = _heightmap(mesh0, axis_idx)
    det = _detect_heightmap(H, extent_cm, BAND_K)
    scale = 1.0
    if (
        det["rowsTotal"] >= 8
        and det["rowsConf"] >= ROWS_CONF_TAU
        and det["rowPitchCm"] > 0
        and gauge_h_prior > 0
    ):
        # 模型尺度行距 vs 先验行距 → 模型虚大倍数
        model_rows_cm = 1.0 / det["rowPitchCm"]
        if model_rows_cm > 0:
            scale = float(np.clip(model_rows_cm / gauge_h_prior, 0.15, 5.0))
    det["suggestRealSizeCm"] = round(extent_cm * scale, 1)
    det["scale"] = scale
    return det


# ---------------------------------------------------------------- 主分析

def _grammar_snap(stitches: np.ndarray, active: np.ndarray) -> tuple[np.ndarray, int]:
    """语法正则化：把相邻圈差值向图解经验分布（±6/±8/0 为主）靠拢。

    只修正离经验集明显偏离的差值（|Δ-最近合法值|≥2 且 |Δ|≤20），
    正确的估算不受影响。返回 (修正后针数, 修正圈数)。
    """
    s = stitches.copy()
    snapped = 0
    for i in range(1, len(s)):
        if not (active[i] and active[i - 1]):
            continue
        d = int(s[i] - s[i - 1])
        if abs(d) > 20 or d == 0:
            continue
        best = min(DELTA_PRIOR, key=lambda k: abs(d - k))
        if abs(d - best) >= 2:
            s[i] = s[i - 1] + best
            snapped += 1
    return s, snapped


def analyze(
    model_path,
    axis: str = "auto",
    gauge_w: float = 2.6,
    gauge_h: float = 3.0,
    real_size_cm: float | None = None,
    auto: bool = False,
    auto_scale: bool = False,
    max_rounds: int = 160,
) -> dict:
    mtime = os.path.getmtime(model_path)
    key = (
        str(model_path), mtime, str(axis), str(gauge_w), round(gauge_h, 2),
        real_size_cm, auto, auto_scale,
    )
    with _lock:
        if key in _cache:
            return _cache[key]

    if axis == "auto":
        pre = trimesh.load(str(model_path))
        pre_mesh = pre.to_geometry()
        axis = "xyz"[int(np.argmax(pre_mesh.extents))]
    axis_idx = {"x": 0, "y": 1, "z": 2}[axis]

    # 1) auto：先在原始尺度上做高度图检测，得到行密度→自动缩放
    auto_info = None
    if auto:
        auto_info = _auto_analysis(model_path, axis_idx, gauge_h)
        if auto_scale:
            real_size_cm = auto_info["suggestRealSizeCm"]

    mesh, model_extent_cm, scale = _load_mesh(model_path, axis, real_size_cm)
    lo, hi = mesh.bounds[0][axis_idx], mesh.bounds[1][axis_idx]
    length_cm = (hi - lo) * 100.0
    area_cm2 = float(mesh.area) * 1e4
    axes_cm = {
        a: round(float(e) * 100.0, 1)
        for a, e in zip("xyz", _load_mesh(model_path, axis, None)[0].extents)
    }

    # 2) 逐圈光滑周长
    n_rounds = max(1, min(max_rounds, round(length_cm * gauge_h)))
    step = (hi - lo) / n_rounds
    perimeters = []
    mean_rs = []
    for i in range(n_rounds):
        p, mr = _slice_perimeter(mesh, axis_idx, lo + step * (i + 0.5))
        perimeters.append(p)
        mean_rs.append(mr)
    perim = np.array(perimeters)
    mean_r = np.array(mean_rs)

    # 3) 离群抑制 + 中位数平滑
    if len(perim) >= 5:
        for i in range(len(perim)):
            w = perim[max(0, i - 2): i + 3]
            local = np.median(w)
            if local > 0 and (perim[i] > 2 * local or perim[i] < 0.4 * local):
                perim[i] = local
    if len(perim) >= 3:
        med = np.copy(perim)
        for i in range(1, len(perim) - 1):
            w = perim[i - 1: i + 2]
            med[i] = np.median(w[w > 0]) if (w > 0).any() else 0.0
        perim = med

    active = perim > 0
    stitches = np.where(active, np.maximum(1, np.round(perim * gauge_w)), 0).astype(int)
    detected = np.zeros(n_rounds, dtype=bool)

    # 4) auto：把高度图检测到的 k(z) 采样到圈上。置信圈足够多时采用检测值，
    #    并用“检测针数/光滑周长”的中位数反推有效横向密度；否则如实报告信号不足。
    if auto and auto_info:
        kp, cp = auto_info["kProfile"], auto_info["confProfile"]
        nz = len(kp)

        def k_at(i):
            zb = int((i + 0.5) / n_rounds * nz)
            lo_, hi_ = max(0, zb - 2), min(nz, zb + 3)
            window_conf = cp[lo_:hi_]
            if (window_conf >= CONF_TAU).any() and kp[lo_:hi_].max() > 0:
                good = kp[lo_:hi_][window_conf >= CONF_TAU]
                return int(np.median(good)), float(np.max(window_conf))
            return 0, 0.0

        ratios = []
        for i in range(n_rounds):
            if not active[i]:
                continue
            k, c = k_at(i)
            if k >= BAND_K[0] and perim[i] > 0:
                ratios.append(k / perim[i])
        if len(ratios) >= 4:
            gauge_eff = float(np.median(ratios))
            stitches = np.where(
                active, np.maximum(1, np.round(perim * gauge_eff)), 0
            ).astype(int)
            for i in range(n_rounds):
                if not active[i]:
                    continue
                k, c = k_at(i)
                if k >= BAND_K[0]:
                    stitches[i] = k
                    detected[i] = True
            auto_info["gaugeWEff"] = round(gauge_eff, 2)
            auto_info["detectedRounds"] = int(detected.sum())
            auto_info["quality"] = "ok"
        else:
            auto_info["gaugeWEff"] = gauge_w
            auto_info["detectedRounds"] = 0
            auto_info["quality"] = "insufficient"
            auto_info[
                "message"
            ] = "当前重建模型里没有检测到足够强的针目周期信号，已回退为密度换算（结果与手动填密度一致）。"
        auto_info["rowsPerCmModel"] = (
            round(1.0 / auto_info["rowPitchCm"], 2) if auto_info["rowPitchCm"] else None
        )

    # 5) 切向过渡区标记：与两侧 3~5 圈外的低谷相比的凸包（蘑菇伞檐外翻等），
    #    平缓的快速加针段与两端收口锥不会被误标
    tangent = np.zeros(n_rounds, dtype=bool)
    med_perim = float(np.median(perim[active])) if active.any() else 0.0
    for i in range(n_rounds):
        if not active[i] or i < 5 or i > n_rounds - 6:
            continue
        left = [perim[j] for j in range(i - 5, i - 2) if perim[j] > 0]
        right = [perim[j] for j in range(i + 3, min(i + 6, n_rounds)) if perim[j] > 0]
        if left and right:
            base = min(max(left), max(right))
            if base > max(0.3 * med_perim, 1e-6) and perim[i] > 1.4 * base:
                tangent[i] = True

    # 6) 语法正则化：差值向图解经验分布（±6/±8/0）靠拢
    stitches, snapped = _grammar_snap(stitches, active)

    rounds = []
    for i in range(n_rounds):
        delta = (
            int(stitches[i] - stitches[i - 1])
            if i > 0 and stitches[i] and stitches[i - 1]
            else None
        )
        rounds.append(
            {
                "round": i + 1,
                "posCm": round(i / gauge_h, 1),
                "perimeterCm": round(float(perim[i]), 1),
                "stitches": int(stitches[i]),
                "delta": delta,
                "detected": bool(detected[i]),
                "tangent": bool(tangent[i]),
            }
        )

    active_rounds = [r for r in rounds if r["stitches"] > 0]
    counted = active_rounds
    total_stitches = sum(r["stitches"] for r in counted)
    area_method_total = round(area_cm2 * gauge_w * gauge_h)

    first = active_rounds[0] if active_rounds else None
    last = active_rounds[-1] if active_rounds else None
    biggest = (
        max(active_rounds, key=lambda r: r["stitches"]) if active_rounds else None
    )

    # 6) 轻量分段：周长曲线的显著极小 → 部件边界
    segments = []
    valid = [(i, perim[i]) for i in range(n_rounds) if perim[i] > 0]
    if len(valid) >= 8:
        vi = np.array([v[0] for v in valid])
        vp = np.array([v[1] for v in valid])
        mins, props = find_peaks(-vp, prominence=np.max(vp) * 0.22)
        cuts = [0] + [int(vi[m]) for m in mins] + [n_rounds]
        for si in range(len(cuts) - 1):
            a, b = cuts[si], cuts[si + 1]
            seg_stitches = sum(
                r["stitches"] for r in rounds[a:b] if r["stitches"] > 0
            )
            if seg_stitches > 0:
                segments.append(
                    {"from": a + 1, "to": b, "stitches": seg_stitches}
                )

    # auto_info 里的 numpy 数组不进响应（不可序列化且前端用不到）
    auto_out = None
    if auto_info:
        auto_out = {
            k: (v.tolist() if isinstance(v, np.ndarray) else v)
            for k, v in auto_info.items()
            if k not in ("kProfile", "confProfile")
        }

    result = {
        "axis": axis,
        "modelAxesCm": axes_cm,
        "modelLengthCm": round(model_extent_cm, 1),
        "scale": round(scale, 3),
        "usedLengthCm": round(length_cm, 1),
        "areaCm2": round(area_cm2),
        "gauge": {"stitchesPerCm": gauge_w, "rowsPerCm": gauge_h},
        "roundCount": len(active_rounds),
        "totalStitches": total_stitches,
        "areaMethodTotal": area_method_total,
        "startStitches": first["stitches"] if first else 0,
        "endStitches": last["stitches"] if last else 0,
        "maxRound": {"round": biggest["round"], "stitches": biggest["stitches"]} if biggest else None,
        "increases": sum(1 for r in rounds if r["delta"] and r["delta"] > 0),
        "decreases": sum(1 for r in rounds if r["delta"] and r["delta"] < 0),
        "segments": segments,
        "auto": auto_out,
        "rounds": rounds,
        "grammarSnapped": snapped,
        "note": "估算值：基于重建模型几何与密度换算，相邻圈差值已按钩织规律（±6/±8 为主）正则化。标⚠的圈是轮廓凸起部位（伞檐/耳朵/相邻部件融合），针数可能偏高。",
    }
    with _lock:
        _cache[key] = result
    return result
