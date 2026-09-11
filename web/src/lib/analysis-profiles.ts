import type { AnalysisOptions, AnalysisProfileCapabilities, VideoAnalysis } from './review-types';

export function defaultAnalysisOptions(capabilities?: AnalysisProfileCapabilities, analysis?: VideoAnalysis | null): AnalysisOptions {
  const prior = analysis?.profile;
  const mode = prior?.resolved_mode ?? 'motion';
  const confidence = prior?.confidence_threshold ?? capabilities?.confidence_threshold.default ?? .35;
  return {mode, sample_fps: prior?.sample_fps ?? 2, ...(mode === 'onnx' ? {confidence_threshold: confidence} : {})};
}

export function analysisOptionsProblem(options: AnalysisOptions, capabilities?: AnalysisProfileCapabilities): string | null {
  if (!capabilities) return 'Refresh the footage library to load the available analysis controls.';
  if (options.mode !== 'motion' && options.mode !== 'onnx') return 'Choose a detector explicitly for this run.';
  if (!capabilities.models.some(model => model.mode === options.mode && model.available)) return 'The selected detector is unavailable on this installation.';
  if (!capabilities.sample_fps.includes(options.sample_fps)) return 'Choose an available sampling rate.';
  if (options.mode === 'onnx') {
    const value = options.confidence_threshold;
    if (value == null || !Number.isFinite(value) || value < capabilities.confidence_threshold.min || value > capabilities.confidence_threshold.max) return 'Set a confidence threshold within the supported range.';
  }
  return null;
}

export function normalizedAnalysisOptions(options: AnalysisOptions): AnalysisOptions {
  return {mode: options.mode, sample_fps: options.sample_fps, ...(options.mode === 'onnx' ? {confidence_threshold: options.confidence_threshold} : {})};
}

export function sameAnalysisOptions(a: AnalysisOptions, b: AnalysisOptions): boolean {
  return JSON.stringify(normalizedAnalysisOptions(a)) === JSON.stringify(normalizedAnalysisOptions(b));
}
