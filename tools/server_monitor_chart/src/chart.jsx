import React from "react";
import ReactDOM from "react-dom";
import { Chart, Coordinate, Interval, Axis, Tooltip, Legend } from "bizcharts";

const METRIC_LABELS = {
  cpu: "CPU",
  mem: "MEM",
  disk: "DISK",
};

const METRIC_COLORS = {
  cpu: "#3B82F6",
  mem: "#10B981",
  disk: "#F59E0B",
};

function formatPercent(value) {
  if (value === null || value === undefined) {
    return "N/A";
  }
  return `${Number(value).toFixed(1)}%`;
}

function formatLoad(values) {
  if (!values || values.length !== 3 || values.some((item) => item === null || item === undefined)) {
    return "N/A";
  }
  return values.map((item) => Number(item).toFixed(2)).join("/");
}

function formatSupervisor(value) {
  if (!value || value.running === null || value.running === undefined || value.total === null || value.total === undefined) {
    return "N/A";
  }
  return `${value.running}/${value.total}`;
}

function computeSummary(metrics) {
  const ok = metrics.filter((item) => item.state === "ok").length;
  const warn = metrics.filter((item) => item.state === "warn").length;
  const fail = metrics.filter((item) => item.state === "fail").length;

  let headline = "整体正常";
  if (fail > 0) {
    headline = `${fail} 个地区采集失败`;
  } else if (warn > 0) {
    headline = `${warn} 个地区触发告警`;
  }

  return { ok, warn, fail, headline };
}

function buildChartData(metrics) {
  return metrics
    .filter((item) => item.state !== "fail")
    .flatMap((item) => [
      { region: item.region.toUpperCase(), metric: METRIC_LABELS.cpu, value: item.cpu_percent ?? 0, rawMetric: "cpu" },
      { region: item.region.toUpperCase(), metric: METRIC_LABELS.mem, value: item.mem_percent ?? 0, rawMetric: "mem" },
      { region: item.region.toUpperCase(), metric: METRIC_LABELS.disk, value: item.disk_percent ?? 0, rawMetric: "disk" },
    ]);
}

function StatusBadge({ item }) {
  const tone = {
    ok: { bg: "#ECFDF5", fg: "#047857", text: "正常" },
    warn: { bg: "#FFF7ED", fg: "#C2410C", text: "告警" },
    fail: { bg: "#FEF2F2", fg: "#B91C1C", text: "失败" },
  }[item.state];

  return (
    <span
      style={{
        background: tone.bg,
        color: tone.fg,
        borderRadius: 999,
        fontSize: 12,
        fontWeight: 700,
        padding: "4px 10px",
        lineHeight: "18px",
      }}
    >
      {tone.text}
    </span>
  );
}

function RegionCard({ item }) {
  return (
    <div
      style={{
        border: "1px solid #E5E7EB",
        borderRadius: 14,
        padding: "14px 16px",
        background: "#FFFFFF",
        minHeight: 112,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 10,
        }}
      >
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, color: "#111827", lineHeight: "22px" }}>
            {item.region}
          </div>
          <div style={{ fontSize: 12, color: "#6B7280", marginTop: 2 }}>{item.label}</div>
        </div>
        <StatusBadge item={item} />
      </div>

      {item.state === "fail" ? (
        <div style={{ fontSize: 12, color: "#B91C1C", lineHeight: "18px" }}>
          {item.error || "采集失败"}
        </div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          <div style={{ fontSize: 12, color: "#6B7280" }}>
            负载
            <div style={{ fontSize: 15, fontWeight: 700, color: "#111827", marginTop: 2 }}>
              {formatLoad(item.load)}
            </div>
          </div>
          <div style={{ fontSize: 12, color: "#6B7280" }}>
            进程
            <div style={{ fontSize: 15, fontWeight: 700, color: "#111827", marginTop: 2 }}>
              {formatSupervisor(item.supervisor)}
            </div>
          </div>
          <div style={{ fontSize: 12, color: "#6B7280" }}>
            CPU
            <div style={{ fontSize: 15, fontWeight: 700, color: "#111827", marginTop: 2 }}>
              {formatPercent(item.cpu_percent)}
            </div>
          </div>
          <div style={{ fontSize: 12, color: "#6B7280" }}>
            磁盘
            <div style={{ fontSize: 15, fontWeight: 700, color: "#111827", marginTop: 2 }}>
              {formatPercent(item.disk_percent)}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function PatrolChart({ payload }) {
  const summary = computeSummary(payload.metrics);
  const chartData = buildChartData(payload.metrics);
  const failedRegions = payload.metrics.filter((item) => item.state === "fail").map((item) => item.region);
  const warningRegions = payload.metrics.filter((item) => item.state === "warn").map((item) => item.region);

  let subtitle = `正常 ${summary.ok} | 告警 ${summary.warn} | 失败 ${summary.fail}`;
  if (failedRegions.length > 0) {
    subtitle = `采集失败：${failedRegions.join(", ").toUpperCase()}`;
  } else if (warningRegions.length > 0) {
    subtitle = `重点关注：${warningRegions.join(", ").toUpperCase()}`;
  }

  return (
    <div
      id="capture"
      style={{
        width: 1280,
        minHeight: 720,
        boxSizing: "border-box",
        background: "linear-gradient(180deg, #F8FAFC 0%, #FFFFFF 100%)",
        padding: "28px 32px 32px",
        fontFamily: '"Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
        color: "#111827",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          marginBottom: 22,
        }}
      >
        <div>
          <div style={{ fontSize: 30, fontWeight: 800, letterSpacing: "-0.02em", marginBottom: 8 }}>
            服务器巡检
          </div>
          <div style={{ fontSize: 16, color: "#374151", marginBottom: 6 }}>
            {payload.timestamp}
          </div>
          <div style={{ fontSize: 14, color: "#6B7280" }}>
            {summary.headline}，{subtitle}
          </div>
        </div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(3, minmax(92px, 1fr))",
            gap: 10,
          }}
        >
          {[
            { label: "正常", value: summary.ok, color: "#047857", bg: "#ECFDF5" },
            { label: "告警", value: summary.warn, color: "#C2410C", bg: "#FFF7ED" },
            { label: "失败", value: summary.fail, color: "#B91C1C", bg: "#FEF2F2" },
          ].map((item) => (
            <div
              key={item.label}
              style={{
                background: item.bg,
                borderRadius: 16,
                padding: "12px 14px",
                minWidth: 94,
              }}
            >
              <div style={{ fontSize: 12, color: item.color, marginBottom: 4 }}>{item.label}</div>
              <div style={{ fontSize: 28, fontWeight: 800, color: item.color, lineHeight: "30px" }}>
                {item.value}
              </div>
            </div>
          ))}
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 1.35fr) minmax(320px, 0.85fr)",
          gap: 20,
          alignItems: "start",
        }}
      >
        <div
          style={{
            background: "#FFFFFF",
            border: "1px solid #E5E7EB",
            borderRadius: 20,
            padding: "20px 18px 12px",
            boxShadow: "0 16px 36px rgba(15, 23, 42, 0.05)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
            <div style={{ fontSize: 18, fontWeight: 700 }}>资源占用概览</div>
            <div style={{ fontSize: 12, color: "#6B7280" }}>
              阈值：CPU/MEM {payload.thresholds.cpu}% · DISK {payload.thresholds.disk}%
            </div>
          </div>
          <Chart
            height={420}
            autoFit
            data={chartData}
            padding={[20, 40, 56, 70]}
            scale={{
              value: { min: 0, max: 100, nice: false },
            }}
          >
            <Coordinate transpose />
            <Legend
              position="top-left"
              itemName={{
                style: {
                  fill: "#374151",
                  fontSize: 12,
                  fontWeight: 600,
                },
              }}
            />
            <Axis
              name="region"
              label={{
                style: {
                  fill: "#111827",
                  fontSize: 12,
                  fontWeight: 700,
                },
              }}
            />
            <Axis
              name="value"
              label={{
                formatter: (value) => `${value}%`,
                style: {
                  fill: "#6B7280",
                  fontSize: 11,
                },
              }}
              grid={{
                line: {
                  style: {
                    stroke: "#E5E7EB",
                    lineDash: [4, 4],
                  },
                },
              }}
            />
            <Tooltip shared showCrosshairs />
            <Interval
              adjust={[
                {
                  type: "dodge",
                  marginRatio: 0.15,
                },
              ]}
              color={[
                "metric",
                (metric) => {
                  const rawMetric = Object.keys(METRIC_LABELS).find((key) => METRIC_LABELS[key] === metric);
                  return METRIC_COLORS[rawMetric] || "#3B82F6";
                },
              ]}
              position="region*value"
              label={[
                "value",
                {
                  offset: 8,
                  style: {
                    fill: "#111827",
                    fontSize: 11,
                    fontWeight: 600,
                  },
                  content: (origin) => `${Number(origin.value).toFixed(1)}%`,
                },
              ]}
            />
          </Chart>
        </div>

        <div style={{ display: "grid", gap: 12 }}>
          {payload.metrics.map((item) => (
            <RegionCard key={item.region} item={item} />
          ))}
        </div>
      </div>
    </div>
  );
}

function mount() {
  const payload = window.__PATROL_PAYLOAD__;
  ReactDOM.render(<PatrolChart payload={payload} />, document.getElementById("root"));

  window.requestAnimationFrame(() => {
    window.setTimeout(() => {
      window.__PATROL_RENDER_READY__ = true;
    }, 600);
  });
}

mount();
