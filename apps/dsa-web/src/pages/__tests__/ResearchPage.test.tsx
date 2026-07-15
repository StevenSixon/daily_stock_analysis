import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../contexts/UiLanguageContext';
import ResearchPage from '../ResearchPage';

const { getStatus, listJobs, listReports } = vi.hoisted(() => ({
  getStatus: vi.fn(),
  listJobs: vi.fn(),
  listReports: vi.fn(),
}));

vi.mock('../../api/research', () => ({
  researchApi: {
    getStatus,
    listJobs,
    listReports,
    createJob: vi.fn(),
    cancelJob: vi.fn(),
    refreshSecurity: vi.fn(),
    getReport: vi.fn(),
    getReportEvidence: vi.fn(),
    reviewReport: vi.fn(),
  },
}));

function renderPage() {
  return render(<UiLanguageProvider><ResearchPage /></UiLanguageProvider>);
}

describe('ResearchPage', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.localStorage.setItem('dsa.uiLanguage', 'zh');
    vi.clearAllMocks();
    listJobs.mockResolvedValue({ items: [], total: 0 });
    listReports.mockResolvedValue({ items: [], total: 0 });
  });

  it('renders an opt-in disabled state without loading job data', async () => {
    getStatus.mockResolvedValue({
      enabled: false,
      databaseReady: false,
      tushareConfigured: false,
      workerTokenConfigured: false,
      supportedWorkflows: [],
      runTokenBudget: 0,
      monthlyTokenBudget: 0,
      currentMonthTokens: 0,
    });
    renderPage();
    expect(await screen.findByRole('heading', { name: '研究中心' })).toBeInTheDocument();
    expect(screen.getByText('Research 功能当前未启用')).toBeInTheDocument();
    expect(listJobs).not.toHaveBeenCalled();
  });

  it('shows the creation form and empty persistent collections when enabled', async () => {
    getStatus.mockResolvedValue({
      enabled: true,
      databaseReady: true,
      tushareConfigured: true,
      workerTokenConfigured: true,
      supportedWorkflows: ['earnings_deep_dive'],
      runTokenBudget: 50000,
      monthlyTokenBudget: 500000,
      currentMonthTokens: 1200,
    });
    renderPage();
    expect(await screen.findByRole('heading', { name: '研究中心' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '创建任务' })).toBeInTheDocument();
    expect(screen.getByText('暂无研究任务')).toBeInTheDocument();
    expect(screen.getByText('暂无研究报告')).toBeInTheDocument();
    expect(screen.getByText('1,200 / 500,000')).toBeInTheDocument();
  });
});
