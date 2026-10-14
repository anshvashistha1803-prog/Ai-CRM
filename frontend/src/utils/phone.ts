/**
 * Phone number masking and display utilities for HAVN Realty CRM.
 */

export function maskPhone(phone?: string | null, phoneMasked?: string | null): string {
  if (phoneMasked && phoneMasked.trim()) {
    return phoneMasked.trim();
  }
  if (!phone || !phone.trim()) {
    return "No phone";
  }
  const clean = phone.trim();
  if (clean.length <= 4) {
    return "••••";
  }
  // For E.164 (e.g. +919876543210): show country code / prefix (+91) and last 2 digits, masking the middle
  const visibleStart = clean.startsWith("+") ? 3 : 2;
  const visibleEnd = 2;
  if (clean.length <= visibleStart + visibleEnd) {
    return `${clean.slice(0, 1)}••••${clean.slice(-1)}`;
  }
  const prefix = clean.slice(0, visibleStart);
  const suffix = clean.slice(-visibleEnd);
  return `${prefix}••••${suffix}`;
}
