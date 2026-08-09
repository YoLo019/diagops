import { useQuery } from "@tanstack/react-query";
import {
  getLatestOpenRcaBenchmark,
  openRcaArtifactUrl,
  type OpenRcaArtifactName,
  type OpenRcaStrategySummary,
} from "./api";

const artifacts: OpenRcaArtifactName[] = [
  "run-manifest.json",
  "fixed-predictions.csv",
  "adaptive-predictions.csv",
  "v11-agent-predictions.csv",
  "official-report.csv",
  "summary.json",
];

const strategyLabels: Record<string, string> = {
  fixed: "Fixed",
  adaptive: "Adaptive",
  "v11-agent": "V11 Agent",
};

function formatScore(value?: number | null) {
  return value === null || value === undefined ? "待评测" : `${(value * 100).toFixed(1)}%`;
}

function formatCount(value: number) {
  return new Intl.NumberFormat().format(value);
}

function formatDuration(value: number) {
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;
}

function StrategyProgress({
  label,
  value,
}: {
  label: string;
  value: number;
}) {
  return (
    <div className="benchmark-progress-row">
      <span>{label}</span>
      <progress max={1} value={value} aria-label={label} />
      <strong>{formatScore(value)}</strong>
    </div>
  );
}

function scoreCells(strategy: OpenRcaStrategySummary) {
  return (
    <>
      <td>{formatScore(strategy.strict_accuracy)}</td>
      <td>{formatScore(strategy.partial_score)}</td>
      <td>{formatScore(strategy.component_score)}</td>
      <td>{formatScore(strategy.reason_score)}</td>
      <td>{formatScore(strategy.time_score)}</td>
    </>
  );
}

export function OpenRcaBenchmark() {
  const query = useQuery({
    queryKey: ["openrca-benchmark", "latest"],
    queryFn: getLatestOpenRcaBenchmark,
    retry: false,
  });

  if (query.isLoading) {
    return <div className="benchmark-empty">加载 OpenRCA Benchmark...</div>;
  }
  if (query.isError || !query.data) {
    return (
      <div className="benchmark-empty">
        <strong>暂无已冻结的 OpenRCA Benchmark 结果</strong>
        <span>完成正式评测并生成 summary.json 后，结果会显示在这里。</span>
      </div>
    );
  }

  const summary = query.data;
  const strategies = Object.entries(summary.strategies);
  const partitions = Array.from(
    new Set(strategies.flatMap(([, value]) => Object.keys(value.per_partition))),
  ).sort();
  const failedCases = strategies.flatMap(([name, value]) =>
    value.failed_cases.map((item) => ({ ...item, strategy: name })),
  );

  return (
    <section className="benchmark-view" aria-label="OpenRCA Benchmark">
      <header className="benchmark-heading">
        <div>
          <h2>OpenRCA Benchmark</h2>
          <p>
            {summary.model} · Prompt {summary.prompt_version} · {summary.case_count} cases
          </p>
        </div>
        <div className="benchmark-run-meta">
          <span>Run {summary.run_id}</span>
          <span>Commit {summary.git_commit.slice(0, 12)}</span>
          <span>{new Date(summary.completed_at).toLocaleString()}</span>
        </div>
      </header>

      <section className="benchmark-section">
        <h3>结果对比</h3>
        <div className="benchmark-table-wrap">
          <table className="benchmark-table">
            <thead>
              <tr>
                <th>策略</th>
                <th>Strict</th>
                <th>Partial</th>
                <th>Component</th>
                <th>Reason</th>
                <th>Time</th>
              </tr>
            </thead>
            <tbody>
              {strategies.map(([name, strategy]) => (
                <tr key={name}>
                  <th>{strategyLabels[name] ?? name}</th>
                  {scoreCells(strategy)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="benchmark-progress-grid">
          {strategies.map(([name, strategy]) => (
            <div className="benchmark-progress-group" key={name}>
              <strong>{strategyLabels[name] ?? name}</strong>
              <StrategyProgress label="完成率" value={strategy.completion_rate} />
              <StrategyProgress
                label="Evidence 合法率"
                value={strategy.evidence_reference_validity}
              />
            </div>
          ))}
        </div>
      </section>

      <section className="benchmark-section">
        <h3>分系统结果</h3>
        <div className="benchmark-table-wrap">
          <table className="benchmark-table">
            <thead>
              <tr>
                <th>系统</th>
                {strategies.map(([name]) => (
                  <th colSpan={2} key={name}>{strategyLabels[name] ?? name}</th>
                ))}
              </tr>
              <tr>
                <th />
                {strategies.flatMap(([name]) => [
                  <th key={`${name}-strict`}>Strict</th>,
                  <th key={`${name}-partial`}>Partial</th>,
                ])}
              </tr>
            </thead>
            <tbody>
              {partitions.map((partition) => (
                <tr key={partition}>
                  <th>{partition}</th>
                  {strategies.flatMap(([name, strategy]) => {
                    const scores = strategy.per_partition[partition];
                    return [
                      <td key={`${name}-strict`}>{formatScore(scores?.strict_accuracy)}</td>,
                      <td key={`${name}-partial`}>{formatScore(scores?.partial_score)}</td>,
                    ];
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="benchmark-section">
        <h3>运行指标</h3>
        <div className="benchmark-table-wrap">
          <table className="benchmark-table">
            <thead>
              <tr>
                <th>策略</th>
                <th>平均调用</th>
                <th>重复拒绝</th>
                <th>平均耗时</th>
                <th>Tokens (in/out)</th>
                <th>估算成本</th>
                <th>只读违规</th>
              </tr>
            </thead>
            <tbody>
              {strategies.map(([name, strategy]) => (
                <tr key={name}>
                  <th>{strategyLabels[name] ?? name}</th>
                  <td>{strategy.average_tool_calls.toFixed(2)}</td>
                  <td>{formatCount(strategy.duplicate_query_rejections)}</td>
                  <td>{formatDuration(strategy.average_duration_ms)}</td>
                  <td>{formatCount(strategy.input_tokens)} / {formatCount(strategy.output_tokens)}</td>
                  <td>${strategy.estimated_cost.toFixed(4)}</td>
                  <td>{formatCount(strategy.read_only_violations)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="benchmark-section benchmark-bottom-grid">
        <div>
          <h3>失败案例</h3>
          {failedCases.length ? (
            <div className="benchmark-table-wrap">
              <table className="benchmark-table">
                <thead><tr><th>策略</th><th>Case</th><th>类别</th></tr></thead>
                <tbody>
                  {failedCases.map((item) => (
                    <tr key={`${item.strategy}-${item.case_id}`}>
                      <td>{strategyLabels[item.strategy] ?? item.strategy}</td>
                      <td>{item.case_id}</td>
                      <td>{item.category}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="benchmark-muted">无失败案例。</p>
          )}
        </div>
        <div>
          <h3>下载 artifact</h3>
          <div className="benchmark-downloads">
            {artifacts.map((artifact) => (
              <a href={openRcaArtifactUrl(artifact)} key={artifact} download>
                {artifact}
              </a>
            ))}
          </div>
        </div>
      </section>
    </section>
  );
}
