export type ResearchStatus = {
  enabled: boolean;
  databaseReady: boolean;
  tushareConfigured: boolean;
  workerTokenConfigured: boolean;
  pluginSkill?: string | null;
  pluginVersion?: string | null;
  supportedWorkflows: string[];
  runTokenBudget: number;
  monthlyTokenBudget: number;
  currentMonthTokens: number;
};

export type ResearchJob = {
  id: string;
  securityId: string;
  workflow: string;
  workflowVersion: string;
  triggerReason: string;
  sourceEventId?: string | null;
  status: string;
  priority: number;
  traceId: string;
  packId?: string | null;
  retryCount: number;
  maxRetries: number;
  cancelRequestedAt?: string | null;
  errorCode?: string | null;
  errorMessage?: string | null;
  metadata?: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
  startedAt?: string | null;
  finishedAt?: string | null;
};

export type ResearchReport = {
  id: string;
  jobId: string;
  runId: string;
  securityId: string;
  packId: string;
  parentReportId?: string | null;
  reportType: string;
  asOf: string;
  status: string;
  executiveSummary?: string | null;
  structured?: Record<string, unknown>;
  markdown?: string;
  contentSha256: string;
  model?: string | null;
  pluginVersion?: string | null;
  workflowVersion?: string | null;
  reviewNote?: string | null;
  createdAt: string;
  reviewedAt?: string | null;
  publishedAt?: string | null;
};

export type ResearchEvidence = {
  id: number;
  reportId: string;
  evidenceType: string;
  evidenceId: string;
  citationPath?: string | null;
  createdAt: string;
};

export type ResearchListResponse<T> = {
  items: T[];
  total: number;
};

export type CreateResearchJobRequest = {
  securityCode: string;
  workflow: string;
  priceBasis?: 'raw' | 'forward' | 'backward';
};
