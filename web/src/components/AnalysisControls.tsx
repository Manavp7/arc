import type { AnalysisOptions, AnalysisProfileCapabilities, VideoAnalysis } from '../lib/review-types';
import {analysisOptionsProblem} from '../lib/analysis-profiles';
import './footage-workbench.css';

interface Props {capabilities?:AnalysisProfileCapabilities; options:AnalysisOptions; analysis:VideoAnalysis|null; disabled:boolean; historical:boolean; changed:boolean; onChange:(options:AnalysisOptions)=>void; onReset:()=>void}
export function AnalysisControls({capabilities,options,analysis,disabled,historical,changed,onChange,onReset}:Props) {
  const issue=analysisOptionsProblem(options,capabilities);
  const onnx=capabilities?.models.find(model=>model.mode==='onnx');
  return <section className="rv-editor-card fw-analysis" aria-label="Analysis controls">
    <div className="rv-section-title"><div><span className="rv-kicker">ANALYSIS / DETECTOR & SAMPLING</span><h2>{historical?'Settings retained with this run':'Choose how the next run analyzes footage.'}</h2></div>{changed&&<span className="rv-badge">Next-run changes</span>}</div>
    <p className="rv-small">Detector settings are saved when you queue analysis. Existing results and case evidence retain their original settings.</p>
    {!historical&&<fieldset disabled={disabled||!capabilities} className="fw-controls">
      <label>Detector for next run<select value={options.mode} onChange={event=>{const mode=event.target.value as 'motion'|'onnx';onChange({mode,sample_fps:options.sample_fps,...(mode==='onnx'?{confidence_threshold:capabilities?.confidence_threshold.default??.35}:{})});}}>
        <option value="motion" disabled={!capabilities?.models.some(model=>model.mode==='motion'&&model.available)}>Motion regions</option>
        <option value="onnx" disabled={!onnx?.available}>{onnx?.name??'Configured object detector'}{!onnx?.available?' · unavailable':''}</option>
      </select></label>
      <label>Analysis samples per second<select value={options.sample_fps} onChange={event=>onChange({...options,sample_fps:Number(event.target.value) as 1|2})}>{(capabilities?.sample_fps??[1,2]).map(rate=><option key={rate} value={rate}>{rate} sample{rate===1?'':'s'} / second</option>)}</select></label>
      {options.mode==='onnx'&&<label>Detection confidence threshold<input type="number" min={capabilities?.confidence_threshold.min??.05} max={capabilities?.confidence_threshold.max??.95} step=".05" value={options.confidence_threshold??''} onChange={event=>onChange({...options,confidence_threshold:event.target.valueAsNumber})}/></label>}
    </fieldset>}
    {!historical&&<p className="rv-small">{options.mode==='motion'?'Motion measures changing regions. It does not classify objects or produce detector confidence.':'The configured object detector must load successfully. An explicitly selected detector will not silently switch to motion.'} Sampling is separate from playback frame rate; brief incidents may fall between samples.</p>}
    {!historical&&!onnx?.available&&<p className="fw-availability">Object detection unavailable · {onnx?.unavailable_reason??'No compatible configured detector is currently available.'}</p>}
    {!historical&&onnx?.available&&onnx.weights_sha256&&<p className="fw-reference">Available model SHA-256 {onnx.weights_sha256}</p>}
    {analysis&&<div className="fw-retained"><strong>{analysis.status==='queued'?'Queued analysis':analysis.status==='running'?'Running analysis':'Retained analysis'} · {analysis.analysis_id}</strong><p>{analysis.model.name}{analysis.profile?` · ${analysis.profile.sample_fps} sample${analysis.profile.sample_fps===1?'':'s'}/s`:' · Legacy run: sampling settings were not recorded in a profile.'}</p>{analysis.profile?.confidence_threshold!=null&&analysis.model.mode==='onnx'&&<p>Confidence threshold {analysis.profile.confidence_threshold}</p>}{analysis.model.weights_sha256&&<p className="fw-reference">Actual model SHA-256 {analysis.model.weights_sha256}</p>}</div>}
    {changed&&<button className="rv-button" disabled={disabled} onClick={onReset}>Reset next-run settings</button>}
    {!historical&&issue&&<p className="rv-small">{issue}</p>}
  </section>;
}
