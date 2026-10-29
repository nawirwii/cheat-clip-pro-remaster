import React, { useState } from 'react';
import { RefreshCw, AlertTriangle, Download, Loader2 } from 'lucide-react';
import { useLanguage } from '../locales';
import type { BatchRenderProgress } from '../types';
import { downloadFileFromUrl } from '../utils/download';
import styles from './BatchRenderProgressModal.module.css';

interface BatchRenderProgressModalProps {
  isOpen: boolean;
  onClose: () => void;
  progress: BatchRenderProgress | null;
  onRetryClip?: (clipIndex?: number) => void;
}

export const BatchRenderProgressModal: React.FC<BatchRenderProgressModalProps> = ({
  isOpen,
  onClose,
  progress,
  onRetryClip,
}) => {
  const { t } = useLanguage();
  const [downloadingClip, setDownloadingClip] = useState<number | null>(null);
  const [downloadingZip, setDownloadingZip] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  if (!isOpen || !progress) return null;

  const handleDownloadClip = async (url: string, idx: number, title: string) => {
    if (downloadingClip !== null) return;
    setDownloadingClip(idx);
    setDownloadError(null);
    try {
      const safe = (title || `clip_${idx + 1}`).replace(/[\\/*?:\"<>|]/g, '').trim() || `clip_${idx + 1}`;
      await downloadFileFromUrl(url, `${safe}.mp4`);
    } catch (e) {
      setDownloadError(e instanceof Error ? e.message : 'Download failed — backend tidak reachable. Cek app desktop running.');
    } finally {
      setDownloadingClip(null);
    }
  };

  const handleDownloadZip = async () => {
    if (downloadingZip || !progress?.zip_url) return;
    setDownloadingZip(true);
    setDownloadError(null);
    try {
      await downloadFileFromUrl(progress.zip_url, `cheat_clip_pro_${progress.batch_id}.zip`);
    } catch (e) {
      setDownloadError(e instanceof Error ? e.message : 'ZIP download failed.');
    } finally {
      setDownloadingZip(false);
    }
  };

  const isAllDone = progress.overall_status === 'completed' || progress.overall_status === 'error';
  const isProcessing = progress.overall_status === 'running';
  const completedCount = progress.clips.filter(c => c.status === 'completed').length;
  const overallPercent = Math.round((completedCount / (progress.total_clips || 1)) * 100);

  return (
    <div className="modal-backdrop">
      <div className={styles.card} onClick={e => e.stopPropagation()}>
        <div className={styles.header}>
          <div>
            <h3 className={styles.title}>{t.batchProgress.modalTitle}</h3>
            <p className={styles.subtitle}>
              {progress.overall_status === 'error'
                ? (progress.error_message || t.batchProgress.statusFailed)
                : isAllDone
                  ? t.batchProgress.allDoneSubtitle(progress.total_clips)
                  : t.batchProgress.processingSubtitle((progress.current_clip_index || 0) + 1, progress.total_clips)}
            </p>
          </div>
          {isAllDone && (
            <button className={styles.closeBtn} onClick={onClose}>
              ✕
            </button>
          )}
        </div>

        {/* Global Progress Bar */}
        <div className={styles.overallBarWrap}>
          <div className={styles.overallBar}>
            <div
              className={styles.overallFill}
              style={{ width: `${overallPercent}%` }}
            ></div>
          </div>
          <div className={styles.overallMeta}>
            <span>{t.batchProgress.completedMeta(completedCount, progress.total_clips)}</span>
            <span>{overallPercent}%</span>
          </div>
        </div>

        {/* Clips List */}
        <div className={styles.itemsList}>
          {progress.clips.map((clip, idx) => (
            <div key={idx} className={styles.itemRow}>
              <div className={styles.itemColTitle}>
                <span className={styles.itemNum}>#{idx + 1}</span>
                <span className={styles.itemText}>{clip.title}</span>
              </div>

              <div className={styles.itemStatusWrap}>
                {clip.status === 'pending' && <span className={`${styles.statusBadge} ${styles.badgePending}`}>{t.batchProgress.statusWaiting}</span>}
                {clip.status === 'downloading' && (
                  <span className={`${styles.statusBadge} ${styles.badgeActive}`}>{t.batchProgress.statusSlicing}</span>
                )}
                {clip.status === 'transcribing' && (
                  <span className={`${styles.statusBadge} ${styles.badgeActive}`}>{t.batchProgress.statusCaptions}</span>
                )}
                {clip.status === 'rendering' && (
                  <span className={`${styles.statusBadge} ${styles.badgeActive}`}>{t.batchProgress.statusRendering}</span>
                )}
                {clip.status === 'completed' && (
                  <div className={styles.completedActionRow}>
                    <span className={`${styles.statusBadge} ${styles.badgeSuccess}`}>{t.batchProgress.statusDone}</span>
                    {clip.download_url && (
                      <button
                        type="button"
                        onClick={() => handleDownloadClip(clip.download_url as string, idx, clip.title)}
                        disabled={downloadingClip !== null}
                        className={styles.btnDownloadClip}
                        title={t.batchProgress.downloadMp4Tooltip}
                        style={{ display: 'inline-flex', alignItems: 'center', gap: '0.3rem', cursor: 'pointer' }}
                      >
                        {downloadingClip === idx ? <Loader2 size={12} className="spin" /> : <Download size={12} />}
                        {downloadingClip === idx ? '...' : '[v] MP4'}
                      </button>
                    )}
                  </div>
                )}
                {clip.status === 'error' && (
                  <div className={styles.itemErrorCol}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                      <span className={`${styles.statusBadge} ${styles.badgeError}`} title={clip.error_message || clip.error}>
                        {t.batchProgress.statusFailed}
                      </span>
                      {onRetryClip && !isProcessing && (
                        <button
                          type="button"
                          onClick={() => onRetryClip(idx)}
                          className={styles.retrySingleBtn}
                          title="Retry rendering this clip"
                          style={{ display: 'inline-flex', alignItems: 'center', gap: '0.25rem' }}
                        >
                          <RefreshCw size={11} />
                          {t.batchProgress.retryClip || 'Retry'}
                        </button>
                      )}
                    </div>
                    {(clip.error_message || clip.error) && (
                      <span
                        className={styles.errorMsgBox}
                        title="Click to copy full error message"
                        onClick={() => navigator.clipboard.writeText(clip.error_message || clip.error || '')}
                        style={{ display: 'inline-flex', alignItems: 'center', gap: '0.25rem', justifyContent: 'flex-end' }}
                      >
                        <AlertTriangle size={11} color="#ff1e27" style={{ flexShrink: 0 }} />
                        <span>{clip.error_message || clip.error}</span>
                      </span>
                    )}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>

        {/* Footer Actions */}
        <div className={styles.footer}>
          {isAllDone ? (
            <>
              {progress.clips.some(c => c.status === 'error') && onRetryClip && (
                <button
                  type="button"
                  onClick={() => onRetryClip()}
                  className={styles.retryAllBtn}
                  title="Retry all failed clips"
                  style={{ display: 'inline-flex', alignItems: 'center', gap: '0.35rem' }}
                >
                  <RefreshCw size={13} />
                  {t.batchProgress.retryAllFailed
                    ? t.batchProgress.retryAllFailed(progress.clips.filter(c => c.status === 'error').length)
                    : `Retry Failed (${progress.clips.filter(c => c.status === 'error').length})`}
                </button>
              )}
              {progress.zip_url && (
                <button
                  type="button"
                  onClick={handleDownloadZip}
                  disabled={downloadingZip}
                  className={styles.zipDownloadBtn}
                  style={{ display: 'inline-flex', alignItems: 'center', gap: '0.35rem', cursor: 'pointer' }}
                >
                  {downloadingZip ? <Loader2 size={13} className="spin" /> : <Download size={13} />}
                  {downloadingZip ? '...' : t.batchProgress.downloadZip}
                </button>
              )}
              {downloadError && (
                <span style={{ fontSize: '0.7rem', color: '#f87171', fontWeight: 600 }} title={downloadError}>
                  {downloadError.length > 80 ? downloadError.slice(0, 80) + '...' : downloadError}
                </span>
              )}
              <button className={styles.closeStudioBtn} onClick={onClose}>
                {t.batchProgress.closeStudio}
              </button>
            </>
          ) : (
            <div className={styles.inProgressHint}>
              <span className="spinner-dots"></span>
              <span>{t.batchProgress.hardwareEncodingHint}</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
