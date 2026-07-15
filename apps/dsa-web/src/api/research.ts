import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  CreateResearchJobRequest,
  ResearchEvidence,
  ResearchJob,
  ResearchListResponse,
  ResearchReport,
  ResearchStatus,
} from '../types/research';

export const researchApi = {
  async getStatus(): Promise<ResearchStatus> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/research/status');
    return toCamelCase<ResearchStatus>(response.data);
  },

  async listJobs(params: { status?: string; page?: number; pageSize?: number } = {}): Promise<ResearchListResponse<ResearchJob>> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/research/jobs', {
      params: { status: params.status, page: params.page ?? 1, page_size: params.pageSize ?? 50 },
    });
    return toCamelCase<ResearchListResponse<ResearchJob>>(response.data);
  },

  async createJob(payload: CreateResearchJobRequest): Promise<{ job: ResearchJob; created: boolean }> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/research/jobs', {
      security_code: payload.securityCode,
      workflow: payload.workflow,
      price_basis: payload.priceBasis ?? 'raw',
    });
    return toCamelCase<{ job: ResearchJob; created: boolean }>(response.data);
  },

  async cancelJob(jobId: string): Promise<ResearchJob> {
    const response = await apiClient.post<Record<string, unknown>>(
      `/api/v1/research/jobs/${encodeURIComponent(jobId)}/cancel`,
    );
    return toCamelCase<ResearchJob>(response.data);
  },

  async refreshSecurity(securityCode: string): Promise<Record<string, unknown>> {
    const response = await apiClient.post<Record<string, unknown>>(
      `/api/v1/research/securities/${encodeURIComponent(securityCode)}/refresh`,
      { years: 5, price_basis: 'raw', include_disclosures: true },
      { timeout: 180000 },
    );
    return toCamelCase<Record<string, unknown>>(response.data);
  },

  async listReports(params: { status?: string; page?: number; pageSize?: number } = {}): Promise<ResearchListResponse<ResearchReport>> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/research/reports', {
      params: { status: params.status, page: params.page ?? 1, page_size: params.pageSize ?? 50 },
    });
    return toCamelCase<ResearchListResponse<ResearchReport>>(response.data);
  },

  async getReport(reportId: string): Promise<ResearchReport> {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/research/reports/${encodeURIComponent(reportId)}`,
    );
    return toCamelCase<ResearchReport>(response.data);
  },

  async getReportEvidence(reportId: string): Promise<ResearchEvidence[]> {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/research/reports/${encodeURIComponent(reportId)}/evidence`,
    );
    return toCamelCase<{ items: ResearchEvidence[] }>(response.data).items;
  },

  async reviewReport(
    reportId: string,
    decision: 'approve' | 'reject' | 'request_changes',
    note?: string,
  ): Promise<ResearchReport> {
    const response = await apiClient.post<Record<string, unknown>>(
      `/api/v1/research/reports/${encodeURIComponent(reportId)}/review`,
      { decision, note: note || null },
    );
    return toCamelCase<ResearchReport>(response.data);
  },
};
