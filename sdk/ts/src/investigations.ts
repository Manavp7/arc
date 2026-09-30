/** Typed review/operations namespace. All requests use the parent client's authentication. */
export interface ReviewVideo {
    video_id: string;
    title: string;
    duration_s: number;
    status: string;
    revision: number;
    [key: string]: unknown;
}
export interface VideoPage {
    videos: ReviewVideo[];
    capabilities: Record<string, unknown>;
    next_cursor?: string | null;
}
export interface VideoJob {
    job_id: string;
    video_id: string;
    video_title: string;
    analysis_id: string;
    revision: number;
    status: 'queued' | 'running' | 'completed' | 'failed' | 'interrupted' | 'cancelled' | 'cancelling';
    progress: number;
    attempt: number;
    max_attempts: number;
    queued_at: string;
    started_at?: string | null;
    finished_at?: string | null;
    error?: string | null;
    retry_of?: string | null;
    analysis_ids?: string[];
    [key: string]: unknown;
}
export interface JobPage {
    jobs: VideoJob[];
    next_cursor: string | null;
    capabilities: {
        max_attempts: number;
        max_pending: number;
        worker_concurrency: number;
    };
}
export interface RetainedAnalysis {
    analysis_id: string;
    status: string;
    video_id?: string;
    created_at?: string;
    model?: {
        name?: string;
        mode?: string;
    };
    zones?: Record<string, unknown>[];
    [key: string]: unknown;
}
export interface AnalysisPage {
    analyses: RetainedAnalysis[];
    next_cursor: string | null;
}
export interface CaseRecord {
    case_id: string;
    title: string;
    status: string;
    revision: number;
    owner?: string | null;
    summary?: string;
    evidence?: Record<string, unknown>;
    [key: string]: unknown;
}
export interface CasePage {
    cases: CaseRecord[];
    next_cursor?: string | null;
}
export interface CaseCreate {
    video_id?: string;
    analysis_id?: string;
    event_id?: string;
    alert_id?: string;
    annotation_set_id?: string;
    annotation_id?: string;
    evaluation_report_id?: string;
    title?: string;
}
export interface CaseUpdate {
    expected_revision: number;
    title?: string;
    owner?: string | null;
    due_at?: string | null;
    status?: 'open' | 'investigating' | 'resolved';
    summary?: string;
    verdict?: 'unreviewed' | 'confirmed' | 'false_positive';
    resolution_note?: string;
}
export interface SearchIndex {
    video_id: string;
    analysis_id: string;
    status: string;
    progress: number | null;
    error: string | null;
    [key: string]: unknown;
}
export interface SearchCatalog {
    videos: {
        video_id: string;
        title: string;
        analyses: {
            analysis_id: string;
            created_at: string;
        }[];
        index: SearchIndex | null;
    }[];
    model: {
        available: boolean;
        model_id: string;
        reason: string | null;
    };
    busy: boolean;
    possibly_truncated: boolean;
    note: string;
}
export interface RecordedSample {
    video_id: string;
    analysis_id: string;
    frame_index: number;
}
export interface RecordedMatch extends RecordedSample {
    video_title: string;
    at_s: number;
    similarity: number;
    frame_url: string;
    events: {
        event_id: string;
        event_type: string;
    }[];
}
export type RecordedSearchQuery = ({
    text: string;
    sample?: never;
} | {
    sample: RecordedSample;
    text?: never;
}) & {
    video_id?: string;
    limit?: number;
};
export interface RecordedSearchResult {
    results: RecordedMatch[];
    total_samples: number;
    searched_recordings: number;
    skipped_recordings: number;
    possibly_truncated: boolean;
    model_id: string;
    note: string;
}
export interface RecordingClock {
    revision: number;
    camera_id: string;
    capture_started_at: string | null;
    clock_offset_s: number;
    uncertainty_s: number | null;
    note: string;
    declared_by?: string;
    updated_at?: string;
}
export interface TimelineRecording {
    video_id: string;
    title: string;
    duration_s: number;
    analysis_id: string | null;
    clock: RecordingClock | null;
    interval: {
        start: string;
        end: string;
    } | null;
}
export interface RecordingTimeline {
    recordings: TimelineRecording[];
    next_cursor: string | null;
    camera_ids: string[];
    note: string;
}
export interface TimelineQuery {
    camera_id?: string;
    from?: string;
    to?: string;
    cursor?: string;
    limit?: number;
}
export interface ActivationPreview {
    preview_id: string;
    source_id: string;
    action: 'activate' | 'rollback';
    expires_at: string;
    before: Record<string, unknown> | null;
    after: Record<string, unknown> | null;
    changed_fields: string[];
    warnings: string[];
    requires_fresh_observation: boolean;
}
export interface ActivationStatus {
    activation_id: string | null;
    source_id: string | null;
    status: string | null;
    message: string | null;
    rollback_available: boolean | null;
    verified_at: string | null;
    sample?: Record<string, unknown> | null;
}
export interface DeliveryHistory {
    history_id?: number;
    kind: string;
    at: string;
    attempt: number;
    actor: string | null;
    reason: string | null;
    error: string | null;
    status_code: number | null;
}
export type DeliveryStatus = 'pending' | 'sending' | 'delivered' | 'failed' | 'blocked';
export interface AlertDelivery {
    delivery_id: string;
    alert_id: string;
    status: DeliveryStatus;
    attempts: number;
    can_retry: boolean;
    history: DeliveryHistory[];
    history_next_cursor: string | null;
    [key: string]: unknown;
}
export interface DeliveryPage {
    deliveries: AlertDelivery[];
    next_cursor: string | null;
    configured: boolean;
    destination: string | null;
    max_attempts: number;
}
export interface HistoryPage {
    history: DeliveryHistory[];
    next_cursor: string | null;
}
export interface CalibrationPreview {
    preview_id: string;
    operation: 'apply' | 'rollback';
    setup_id: string;
    setup_revision: number;
    source_id: string;
    expires_at: string;
    previous_pose: Record<string, unknown> | null;
    proposed_pose: Record<string, unknown> | null;
    note: string;
}
export interface CalibrationStatus {
    publication_id: string | null;
    status: 'not_published' | 'pending_fusion' | 'applied' | 'rolled_back' | 'superseded';
    source_id: string;
    calibration_revision: number | null;
    applied_at: string | null;
    applied_by: string | null;
    fusion: {
        acknowledged: boolean;
        acknowledged_at: string | null;
    };
    can_rollback: boolean;
    note: string;
}
interface Transport {
    request<T>(method: string, path: string, init?: {
        query?: Record<string, unknown>;
        body?: unknown;
        raw?: RequestInit;
    }): Promise<T>;
}
const id = encodeURIComponent;
export class InvestigationClient {
    constructor(private readonly transport: Transport) { }
    videos(): Promise<VideoPage> { return this.transport.request('GET', '/api/review/videos'); }
    video(videoId: string): Promise<ReviewVideo> { return this.transport.request('GET', `/api/review/videos/${id(videoId)}`); }
    upload(content: Blob, filename: string, accessZoneId?: string, signal?: AbortSignal): Promise<ReviewVideo> { return this.transport.request('POST', '/api/review/videos', { query: { access_zone_id: accessZoneId }, raw: { body: content, headers: { 'Content-Type': 'video/mp4', 'X-Filename': encodeURIComponent(filename) }, signal } }); }
    jobs(query: {
        view?: 'all' | 'active' | 'finished';
        cursor?: string;
        limit?: number;
    } = {}): Promise<JobPage> { return this.transport.request('GET', '/api/review/jobs', { query: { view: 'all', limit: 100, ...query } }); }
    analyses(videoId: string, page: {
        cursor?: string;
        limit?: number;
    } = {}): Promise<AnalysisPage> { return this.transport.request('GET', `/api/review/videos/${id(videoId)}/analyses`, { query: { limit: 50, ...page } }); }
    analysis(videoId: string, analysisId: string): Promise<RetainedAnalysis> { return this.transport.request('GET', `/api/review/videos/${id(videoId)}/analysis`, { query: { analysis_id: analysisId } }); }
    cases(): Promise<CasePage> { return this.transport.request('GET', '/api/cases'); }
    case(caseId: string): Promise<CaseRecord> { return this.transport.request('GET', `/api/cases/${id(caseId)}`); }
    createCase(body: CaseCreate): Promise<CaseRecord> { return this.transport.request('POST', '/api/cases', { body }); }
    updateCase(caseId: string, body: CaseUpdate): Promise<CaseRecord> { return this.transport.request('PATCH', `/api/cases/${id(caseId)}`, { body }); }
    searchCatalog(): Promise<SearchCatalog> { return this.transport.request('GET', '/api/review/recorded-search/catalog'); }
    indexRecording(videoId: string, analysisId: string, consentPrivateOriginal: true): Promise<SearchIndex> { if (consentPrivateOriginal !== true)
        return Promise.reject(new Error('Explicit consent to local processing of the private original is required')); return this.transport.request('POST', '/api/review/recorded-search/index', { body: { video_id: videoId, analysis_id: analysisId, consent_private_original: true } }); }
    searchRecordings(query: RecordedSearchQuery): Promise<RecordedSearchResult> { if (Boolean(query.text?.trim()) === Boolean(query.sample))
        return Promise.reject(new Error('Provide exactly one nonempty text query or sample reference')); return this.transport.request('POST', '/api/review/recorded-search/query', { body: { text: query.text?.trim() ?? '', sample: query.sample ?? null, video_id: query.video_id ?? '', limit: query.limit ?? 24 } }); }
    async removeSearchIndex(videoId: string): Promise<boolean> { const result = await this.transport.request<{
        removed: boolean;
    }>('DELETE', `/api/review/recorded-search/index/${id(videoId)}`); return result.removed; }
    timeline(query: TimelineQuery = {}): Promise<RecordingTimeline> { return this.transport.request('GET', '/api/review/recording-timeline', { query: { limit: 20, ...query } }); }
    saveClock(videoId: string, clock: RecordingClock): Promise<RecordingClock> { const { revision, camera_id, capture_started_at, clock_offset_s, uncertainty_s, note } = clock; return this.transport.request('PUT', `/api/review/videos/${id(videoId)}/clock`, { body: { revision, camera_id, capture_started_at, clock_offset_s, uncertainty_s, note } }); }
    activationStatus(sourceId: string): Promise<ActivationStatus> { return this.transport.request('GET', `/api/sources/${id(sourceId)}/activation`); }
    previewActivation(sourceId: string, rollback = false): Promise<ActivationPreview> { return this.transport.request('POST', `/api/sources/${id(sourceId)}/activation/${rollback ? 'rollback-preview' : 'preview'}`, { body: {} }); }
    applyActivation(preview: ActivationPreview, timeoutS = 20): Promise<ActivationStatus> { return this.transport.request('POST', `/api/sources/${id(preview.source_id)}/activation${preview.action === 'rollback' ? '/rollback' : ''}`, { body: { preview_id: preview.preview_id, timeout_s: timeoutS } }); }
    deliveries(query: {
        status?: DeliveryStatus;
        alert_id?: string;
        cursor?: string;
        limit?: number;
    } = {}): Promise<DeliveryPage> { return this.transport.request('GET', '/api/alert-deliveries', { query: { limit: 100, ...query } }); }
    deliveryHistory(deliveryId: string, page: {
        cursor?: string;
        limit?: number;
    } = {}): Promise<HistoryPage> { return this.transport.request('GET', `/api/alert-deliveries/${id(deliveryId)}/history`, { query: { limit: 20, ...page } }); }
    retryDelivery(deliveryId: string, reason: string): Promise<AlertDelivery> { return this.transport.request('POST', `/api/alert-deliveries/${id(deliveryId)}/retry`, { body: { reason: reason.trim() } }); }
    calibrationStatus(setupId: string): Promise<CalibrationStatus> { return this.transport.request('GET', `/api/camera-setups/${id(setupId)}/calibration/status`); }
    previewCalibration(setupId: string, expectedRevision: number, rollback = false): Promise<CalibrationPreview> { return this.transport.request('POST', `/api/camera-setups/${id(setupId)}/calibration/${rollback ? 'rollback-preview' : 'preview'}`, { body: { expected_revision: expectedRevision } }); }
    applyCalibration(preview: CalibrationPreview): Promise<CalibrationStatus> { return this.transport.request('POST', `/api/camera-setups/${id(preview.setup_id)}/calibration/${preview.operation}`, { body: { preview_id: preview.preview_id, expected_revision: preview.setup_revision } }); }
}
