// Shared dialog keyboard navigation and focus restoration.
const MODAL_FOCUS_SELECTOR = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';
let modalReturnFocusTarget = null;

function syncModalScrollLock() {
  document.documentElement.classList.toggle('modal-open', Boolean(activeModalOverlay()));
}

function getFocusableModalElements(modal) {
  return Array.from(modal?.querySelectorAll(MODAL_FOCUS_SELECTOR) || [])
    .filter(el => !el.disabled && el.offsetParent !== null);
}

function getFocusableModalElement(modal) {
  return getFocusableModalElements(modal)[0] || null;
}

function openAccessibleModal(modal, focusTarget = null) {
  if (!modal) return;
  modalReturnFocusTarget = document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null;
  modal.classList.add('show');
  const body = modal.querySelector('.modal-body');
  if (body) body.scrollTop = 0;
  syncModalScrollLock();
  window.setTimeout(() => {
    const target = focusTarget || getFocusableModalElement(modal);
    target?.focus?.();
  }, 0);
}

function closeAccessibleModal(modal, restoreFocus = true) {
  if (!modal) return;
  modal.classList.remove('show');
  syncModalScrollLock();
  if (
    restoreFocus &&
    modalReturnFocusTarget &&
    document.body.contains(modalReturnFocusTarget) &&
    typeof modalReturnFocusTarget.focus === 'function'
  ) {
    modalReturnFocusTarget.focus({ preventScroll: true });
  }
  modalReturnFocusTarget = null;
}

function activeModalOverlay() {
  const modals = Array.from(document.querySelectorAll('.modal-overlay.show'));
  return modals.length ? modals[modals.length - 1] : null;
}

function trapModalFocus(event, modal) {
  if (event.key !== 'Tab' || !modal) return false;
  const focusable = getFocusableModalElements(modal);
  if (!focusable.length) return false;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;

  if (!modal.contains(active)) {
    event.preventDefault();
    first.focus();
    return true;
  }
  if (event.shiftKey && active === first) {
    event.preventDefault();
    last.focus();
    return true;
  }
  if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
    return true;
  }
  return false;
}

function handleModalEscapeKeydown(event) {
  const modal = activeModalOverlay();
  if (!modal) return;
  if (trapModalFocus(event, modal)) return;
  if (event.key !== 'Escape') return;
  const handlers = {
    'upload-modal': closeUploadModal,
    'summary-editor-modal': closeSummaryEditor,
    'transcript-editor-modal': closeTranscriptEditor,
    'revision-modal': closeRevisionHistory,
    'source-storage-modal': closeSourceStorageInventory,
    'record-modal': closeRecordModal
  };
  const close = handlers[modal.id];
  if (!close) return;
  event.preventDefault();
  close();
}

document.addEventListener('keydown', handleModalEscapeKeydown);
