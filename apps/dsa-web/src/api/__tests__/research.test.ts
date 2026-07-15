import { beforeEach, describe, expect, it, vi } from 'vitest';
import { researchApi } from '../research';

const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));

vi.mock('../index', () => ({ default: { get, post } }));

describe('researchApi', () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
  });

  it('normalizes status and persistent job fields', async () => {
    get.mockResolvedValueOnce({
      data: {
        enabled: true,
        database_ready: true,
        worker_token_configured: true,
        supported_workflows: ['earnings_deep_dive'],
        run_token_budget: 50000,
        monthly_token_budget: 500000,
        current_month_tokens: 1200,
      },
    });
    const status = await researchApi.getStatus();
    expect(get).toHaveBeenCalledWith('/api/v1/research/status');
    expect(status.databaseReady).toBe(true);
    expect(status.workerTokenConfigured).toBe(true);
    expect(status.supportedWorkflows).toEqual(['earnings_deep_dive']);
    expect(status.currentMonthTokens).toBe(1200);
  });

  it('creates a snake-case Research job request', async () => {
    post.mockResolvedValueOnce({
      data: {
        created: true,
        job: { id: 'job-1', security_id: 'sec-1', workflow: 'earnings_deep_dive', status: 'data_ready' },
      },
    });
    const response = await researchApi.createJob({
      securityCode: '600519',
      workflow: 'earnings_deep_dive',
      priceBasis: 'raw',
    });
    expect(post).toHaveBeenCalledWith('/api/v1/research/jobs', {
      security_code: '600519',
      workflow: 'earnings_deep_dive',
      price_basis: 'raw',
    });
    expect(response.job.securityId).toBe('sec-1');
  });

  it('loads report evidence through the encoded report path', async () => {
    get.mockResolvedValueOnce({
      data: { items: [{ id: 1, report_id: 'report/1', evidence_id: 'ff:one', evidence_type: 'financial_fact' }] },
    });
    const evidence = await researchApi.getReportEvidence('report/1');
    expect(get).toHaveBeenCalledWith('/api/v1/research/reports/report%2F1/evidence');
    expect(evidence[0].evidenceType).toBe('financial_fact');
  });
});
