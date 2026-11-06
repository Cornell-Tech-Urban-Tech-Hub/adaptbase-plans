// Page scroll lock shared by every modal on the site.
//
// While any modal is open the page behind it must not scroll. Each modal
// calls lockScroll() when it opens and unlockScroll() when it closes; the
// lock is counted, so one modal closing never unlocks the page while another
// is still open. The scrollbar's width is put back as padding, so hiding it
// does not shift the page sideways.

let locks = 0;
let savedOverflow = '';
let savedPaddingRight = '';

export function lockScroll(): void {
  if (locks++ > 0) return;
  const body = document.body;
  const scrollbar = window.innerWidth - document.documentElement.clientWidth;
  savedOverflow = body.style.overflow;
  savedPaddingRight = body.style.paddingRight;
  body.style.overflow = 'hidden';
  if (scrollbar > 0) {
    const current = parseFloat(getComputedStyle(body).paddingRight) || 0;
    body.style.paddingRight = `${current + scrollbar}px`;
  }
}

export function unlockScroll(): void {
  if (locks === 0) return;
  if (--locks > 0) return;
  document.body.style.overflow = savedOverflow;
  document.body.style.paddingRight = savedPaddingRight;
}
