import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { BookOpenCheck, Database, FileSearch, FlaskConical, Gauge, RefreshCw, ShieldCheck } from 'lucide-react';
import { researchApi } from '../api/research';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import {
  ApiErrorAlert,
  AppPage,
  Badge,
  Button,
  Card,
  Drawer,
  EmptyState,
  Input,
  Loading,
  PageHeader,
  Select,
} from '../components/common';
import { ReportMarkdownBody } from '../components/report/ReportMarkdownBody';
import { useUiLanguage } from '../contexts/UiLanguageContext';
import type { ResearchEvidence, ResearchJob, ResearchReport, ResearchStatus } from '../types/research';
import { formatDateTime } from '../utils/format';

const ACTIVE_JOB_STATUSES = new Set(['queued', 'collecting_data', 'data_ready', 'analyzing', 'validating', 'failed_retryable', 'cancel_requested']);

const WORKFLOWS = [
  'earnings_deep_dive',
  'earnings_preview',
  'initiating_coverage',
  'thesis_update',
  'thesis_tracker',
  'catalyst_calendar',
  'dcf',
  'comps_valuation',
  'long_short_pitch',
];

const COPY = {
  zh: {
    title: '研究中心',
    pageTitle: '研究中心 - DSA',
    description: '以冻结的 Evidence Pack 驱动 PEI 深度研究，集中查看持久化任务、证据引用、报告版本和人工审核状态。',
    create: '创建研究任务',
    code: 'A 股代码',
    workflow: '研究工作流',
    submit: '创建任务',
    refresh: '同步研究数据',
    refreshing: '同步中...',
    jobs: '研究任务',
    reports: '研究报告',
    noJobs: '暂无研究任务',
    noReports: '暂无研究报告',
    noJobsHint: '先同步一只 A 股的研究数据，再创建 PEI 工作流。',
    noReportsHint: 'Worker 完成任务并通过服务端校验后，报告会出现在这里。',
    disabled: 'Research 功能当前未启用',
    disabledHint: '在后端配置 RESEARCH_ENABLED=true 后，重新加载页面。默认关闭不会影响现有 DSA 功能。',
    data: '研究数据库',
    worker: 'Worker 令牌',
    tushare: 'Tushare',
    budget: '本月 Token',
    runBudget: '单次上限',
    unlimited: '不限',
    ready: '就绪',
    missing: '未配置',
    cancel: '取消任务',
    details: '查看报告',
    evidence: '证据引用',
    review: '人工审核',
    approve: '批准发布',
    reject: '拒绝',
    changes: '请求修改',
    close: '关闭',
    reviewNote: '审核说明（可选）',
    created: '任务已创建；相同幂等事件不会重复排队。',
    deduplicated: '已返回现有幂等任务，没有重复创建。',
    refreshDone: '研究数据同步完成。',
    retry: '重新加载',
  },
  en: {
    title: 'Research Center',
    pageTitle: 'Research Center - DSA',
    description: 'Run evidence-bounded PEI workflows and review persistent jobs, citations, report versions, and publication status.',
    create: 'Create research job',
    code: 'A-share code',
    workflow: 'Research workflow',
    submit: 'Create job',
    refresh: 'Sync research data',
    refreshing: 'Syncing...',
    jobs: 'Research jobs',
    reports: 'Research reports',
    noJobs: 'No research jobs',
    noReports: 'No research reports',
    noJobsHint: 'Sync research data for an A-share security, then create a PEI workflow.',
    noReportsHint: 'Reports appear after a Worker run passes server-side validation.',
    disabled: 'Research is disabled',
    disabledHint: 'Set RESEARCH_ENABLED=true on the backend and reload. The opt-in feature does not affect existing DSA flows.',
    data: 'Research database',
    worker: 'Worker token',
    tushare: 'Tushare',
    budget: 'Monthly tokens',
    runBudget: 'Per-run limit',
    unlimited: 'Unlimited',
    ready: 'Ready',
    missing: 'Missing',
    cancel: 'Cancel job',
    details: 'View report',
    evidence: 'Evidence citations',
    review: 'Human review',
    approve: 'Approve',
    reject: 'Reject',
    changes: 'Request changes',
    close: 'Close',
    reviewNote: 'Review note (optional)',
    created: 'Job created; duplicate events are idempotently suppressed.',
    deduplicated: 'The existing idempotent job was returned.',
    refreshDone: 'Research data sync completed.',
    retry: 'Reload',
  },
} as const;

function statusVariant(status: string): 'default' | 'success' | 'warning' | 'danger' | 'info' | 'history' {
  if (['published', 'ready', 'data_ready'].includes(status)) return 'success';
  if (['failed_permanent', 'rejected', 'blocked_data', 'cancelled'].includes(status)) return 'danger';
  if (['awaiting_review', 'changes_requested', 'failed_retryable', 'cancel_requested'].includes(status)) return 'warning';
  if (['analyzing', 'validating', 'collecting_data'].includes(status)) return 'info';
  return 'default';
}

const ResearchPage: React.FC = () => {
  const { language } = useUiLanguage();
  const text = COPY[language];
  const [status, setStatus] = useState<ResearchStatus | null>(null);
  const [jobs, setJobs] = useState<ResearchJob[]>([]);
  const [reports, setReports] = useState<ResearchReport[]>([]);
  const [code, setCode] = useState('600519');
  const [workflow, setWorkflow] = useState('earnings_deep_dive');
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [selectedReport, setSelectedReport] = useState<ResearchReport | null>(null);
  const [evidence, setEvidence] = useState<ResearchEvidence[]>([]);
  const [detailLoading, setDetailLoading] = useState(false);
  const [reviewNote, setReviewNote] = useState('');

  useEffect(() => {
    document.title = text.pageTitle;
  }, [text.pageTitle]);

  const load = useCallback(async (showLoading = true) => {
    if (showLoading) setLoading(true);
    try {
      const nextStatus = await researchApi.getStatus();
      setStatus(nextStatus);
      if (nextStatus.enabled) {
        const [jobResponse, reportResponse] = await Promise.all([
          researchApi.listJobs(),
          researchApi.listReports(),
        ]);
        setJobs(jobResponse.items);
        setReports(reportResponse.items);
      } else {
        setJobs([]);
        setReports([]);
      }
      setError(null);
    } catch (caught) {
      setError(getParsedApiError(caught));
    } finally {
      if (showLoading) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const hasActiveJobs = useMemo(() => jobs.some((job) => ACTIVE_JOB_STATUSES.has(job.status)), [jobs]);
  useEffect(() => {
    if (!status?.enabled || !hasActiveJobs) return undefined;
    const timer = window.setInterval(() => void load(false), 10000);
    return () => window.clearInterval(timer);
  }, [hasActiveJobs, load, status?.enabled]);

  const handleCreate = async (event: React.FormEvent) => {
    event.preventDefault();
    setCreating(true);
    setNotice(null);
    try {
      const result = await researchApi.createJob({ securityCode: code.trim(), workflow });
      setNotice(result.created ? text.created : text.deduplicated);
      await load(false);
    } catch (caught) {
      setError(getParsedApiError(caught));
    } finally {
      setCreating(false);
    }
  };

  const handleRefresh = async () => {
    setRefreshing(true);
    setNotice(null);
    try {
      await researchApi.refreshSecurity(code.trim());
      setNotice(text.refreshDone);
      await load(false);
    } catch (caught) {
      setError(getParsedApiError(caught));
    } finally {
      setRefreshing(false);
    }
  };

  const openReport = async (report: ResearchReport) => {
    setDetailLoading(true);
    setReviewNote('');
    try {
      const [detail, reportEvidence] = await Promise.all([
        researchApi.getReport(report.id),
        researchApi.getReportEvidence(report.id),
      ]);
      setSelectedReport(detail);
      setEvidence(reportEvidence);
    } catch (caught) {
      setError(getParsedApiError(caught));
    } finally {
      setDetailLoading(false);
    }
  };

  const review = async (decision: 'approve' | 'reject' | 'request_changes') => {
    if (!selectedReport) return;
    setDetailLoading(true);
    try {
      const updated = await researchApi.reviewReport(selectedReport.id, decision, reviewNote);
      setSelectedReport(updated);
      await load(false);
    } catch (caught) {
      setError(getParsedApiError(caught));
    } finally {
      setDetailLoading(false);
    }
  };

  if (loading) {
    return <AppPage><Loading /></AppPage>;
  }

  if (!status?.enabled) {
    return (
      <AppPage className="space-y-5">
        <PageHeader eyebrow="PEI Research" title={text.title} description={text.description} />
        {error ? <ApiErrorAlert error={error} /> : null}
        <EmptyState
          title={text.disabled}
          description={text.disabledHint}
          icon={<FlaskConical className="h-8 w-8" />}
          action={<Button variant="outline" onClick={() => void load()}>{text.retry}</Button>}
        />
      </AppPage>
    );
  }

  return (
    <AppPage className="space-y-5">
      <PageHeader
        eyebrow="PEI Research"
        title={text.title}
        description={text.description}
        actions={<Button variant="outline" onClick={() => void load()}><RefreshCw className="h-4 w-4" />{text.retry}</Button>}
      />
      {error ? <ApiErrorAlert error={error} onDismiss={() => setError(null)} /> : null}
      {notice ? <Card padding="sm"><p className="text-sm text-success">{notice}</p></Card> : null}

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {[
          { label: text.data, ready: status.databaseReady, Icon: Database },
          { label: text.tushare, ready: status.tushareConfigured, Icon: FileSearch },
          { label: text.worker, ready: status.workerTokenConfigured, Icon: ShieldCheck },
        ].map(({ label, ready, Icon }) => (
          <Card key={label} padding="sm">
            <div className="flex items-center justify-between gap-3">
              <div className="flex items-center gap-3"><Icon className="h-5 w-5 text-cyan" /><span className="text-sm text-foreground">{label}</span></div>
              <Badge variant={ready ? 'success' : 'warning'}>{ready ? text.ready : text.missing}</Badge>
            </div>
          </Card>
        ))}
        <Card padding="sm">
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-3"><Gauge className="h-5 w-5 text-cyan" /><span className="text-sm text-foreground">{text.budget}</span></div>
            <span className="text-sm font-medium text-foreground">
              {status.currentMonthTokens.toLocaleString()} / {status.monthlyTokenBudget ? status.monthlyTokenBudget.toLocaleString() : text.unlimited}
            </span>
          </div>
          <p className="mt-2 text-xs text-secondary-text">{text.runBudget}: {status.runTokenBudget ? status.runTokenBudget.toLocaleString() : text.unlimited}</p>
        </Card>
      </div>

      <Card title={text.create} subtitle="Evidence Pack → PEI">
        <form className="grid gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)_auto_auto] md:items-end" onSubmit={handleCreate}>
          <Input label={text.code} value={code} onChange={(event) => setCode(event.target.value)} required minLength={6} maxLength={32} />
          <Select label={text.workflow} value={workflow} onChange={setWorkflow} options={WORKFLOWS.map((value) => ({ value, label: value }))} />
          <Button type="button" variant="outline" isLoading={refreshing} loadingText={text.refreshing} onClick={() => void handleRefresh()}>{text.refresh}</Button>
          <Button type="submit" isLoading={creating}>{text.submit}</Button>
        </form>
      </Card>

      <div className="grid gap-5 xl:grid-cols-2">
        <Card title={`${text.jobs} · ${jobs.length}`} subtitle="Persistent queue">
          {jobs.length === 0 ? <EmptyState title={text.noJobs} description={text.noJobsHint} /> : (
            <div className="space-y-3">
              {jobs.map((job) => (
                <div key={job.id} className="rounded-xl border border-border/60 bg-elevated/40 p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div><p className="font-medium text-foreground">{job.workflow}</p><p className="mt-1 text-xs text-secondary-text">{job.securityId} · {formatDateTime(job.createdAt)}</p></div>
                    <Badge variant={statusVariant(job.status)}>{job.status}</Badge>
                  </div>
                  {job.errorMessage ? <p className="mt-3 text-xs text-danger">{job.errorCode}: {job.errorMessage}</p> : null}
                  {ACTIVE_JOB_STATUSES.has(job.status) ? <Button className="mt-3" size="sm" variant="danger-subtle" onClick={() => void researchApi.cancelJob(job.id).then(() => load(false)).catch((caught) => setError(getParsedApiError(caught)))}>{text.cancel}</Button> : null}
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card title={`${text.reports} · ${reports.length}`} subtitle="Versioned JSON + Markdown">
          {reports.length === 0 ? <EmptyState title={text.noReports} description={text.noReportsHint} /> : (
            <div className="space-y-3">
              {reports.map((report) => (
                <button key={report.id} type="button" className="w-full rounded-xl border border-border/60 bg-elevated/40 p-4 text-left transition-colors hover:bg-hover" onClick={() => void openReport(report)}>
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div><p className="font-medium text-foreground">{report.reportType}</p><p className="mt-1 text-xs text-secondary-text">as_of {formatDateTime(report.asOf)}</p></div>
                    <Badge variant={statusVariant(report.status)}>{report.status}</Badge>
                  </div>
                  {report.executiveSummary ? <p className="mt-3 line-clamp-3 text-sm text-secondary-text">{report.executiveSummary}</p> : null}
                  <span className="mt-3 inline-flex items-center gap-2 text-xs text-cyan"><BookOpenCheck className="h-4 w-4" />{text.details}</span>
                </button>
              ))}
            </div>
          )}
        </Card>
      </div>

      <Drawer isOpen={selectedReport !== null} onClose={() => setSelectedReport(null)} title={selectedReport?.reportType ?? text.reports} width="max-w-4xl">
        {selectedReport ? (
          <div className="space-y-5">
            <div className="flex flex-wrap gap-2"><Badge variant={statusVariant(selectedReport.status)}>{selectedReport.status}</Badge><Badge>as_of {formatDateTime(selectedReport.asOf)}</Badge><Badge>{selectedReport.contentSha256.slice(0, 18)}…</Badge></div>
            <ReportMarkdownBody content={selectedReport.markdown ?? ''} />
            <Card title={`${text.evidence} · ${evidence.length}`} padding="sm">
              <div className="space-y-2">{evidence.map((item) => <div key={item.id} className="break-all rounded-lg bg-elevated/60 px-3 py-2 text-xs text-secondary-text"><span className="text-cyan">{item.evidenceType}</span> · {item.evidenceId}</div>)}</div>
            </Card>
            {['awaiting_review', 'changes_requested'].includes(selectedReport.status) ? (
              <Card title={text.review} padding="sm">
                <Input label={text.reviewNote} value={reviewNote} onChange={(event) => setReviewNote(event.target.value)} maxLength={4000} />
                <div className="mt-4 flex flex-wrap gap-2"><Button isLoading={detailLoading} onClick={() => void review('approve')}>{text.approve}</Button><Button variant="outline" disabled={detailLoading} onClick={() => void review('request_changes')}>{text.changes}</Button><Button variant="danger-subtle" disabled={detailLoading} onClick={() => void review('reject')}>{text.reject}</Button></div>
              </Card>
            ) : null}
          </div>
        ) : null}
      </Drawer>
    </AppPage>
  );
};

export default ResearchPage;
