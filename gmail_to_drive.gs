/**
 * gmail_to_drive.gs — Gmail → Google Drive receipt bridge (Apps Script).
 *
 * Runs in YOUR Google account on a time trigger (nothing to host, no app
 * credentials). It finds receipt emails you labelled "Receipts", saves their
 * image/PDF attachments (and, optionally, the email body as a PDF) into a Drive
 * folder, then re-labels the thread so it is never processed twice. The receipt
 * app's Drive poller (see gdrive_intake.py) then drains that same folder.
 *
 * One-time setup is in GMAIL_TO_DRIVE_SETUP.md. In short:
 *   1. Create a Gmail filter that applies the label "Receipts" to receipt mail
 *      (by sender / subject). Create the "Receipts" and "Receipts-Saved" labels.
 *   2. Paste this script into script.google.com, set INBOX_FOLDER_ID below.
 *   3. Add a time-driven trigger to run saveReceiptsToDrive() every 15 minutes.
 *
 * Privacy: this script never touches the receipt app and holds no app credential.
 * It only moves mail that is already in your Gmail into your own Drive folder.
 */

// ── Configure these two ─────────────────────────────────────────────────────────
var INBOX_FOLDER_ID = 'YOUR_INBOX_FOLDER_ID';   // the Drive folder the app polls
var SAVE_BODY_AS_PDF = false;                    // also save body-only e-receipts

// Labels (created once in Gmail; see the setup guide).
var SRC_LABEL  = 'Receipts';
var DONE_LABEL = 'Receipts-Saved';


function saveReceiptsToDrive() {
  var folder = DriveApp.getFolderById(INBOX_FOLDER_ID);
  var doneLabel = GmailApp.getUserLabelByName(DONE_LABEL) ||
                  GmailApp.createLabel(DONE_LABEL);

  // Unprocessed receipt mail only. Re-labelling (below) is the dedup, the Apps
  // Script equivalent of tracking Message-IDs; the app's file-ID dedup is the
  // second safety net.
  var threads = GmailApp.search('label:' + SRC_LABEL + ' -label:' + DONE_LABEL, 0, 50);

  for (var t = 0; t < threads.length; t++) {
    var thread = threads[t];
    var messages = thread.getMessages();
    for (var m = 0; m < messages.length; m++) {
      var msg = messages[m];
      var saved = 0;
      var attachments = msg.getAttachments({ includeInlineImages: true });
      for (var a = 0; a < attachments.length; a++) {
        var att = attachments[a];
        var type = att.getContentType() || '';
        if (type.indexOf('image/') === 0 || type === 'application/pdf') {
          folder.createFile(att.copyBlob());   // → the Drive inbox
          saved++;
        }
      }
      // Optional: save the email body as a PDF for body-only digital receipts.
      if (SAVE_BODY_AS_PDF && saved === 0) {
        var html = msg.getBody() || msg.getPlainBody() || '';
        var pdf = Utilities.newBlob(html, 'text/html', 'receipt.html')
                           .getAs('application/pdf')
                           .setName('receipt-' + msg.getId() + '.pdf');
        folder.createFile(pdf);
      }
    }
    thread.addLabel(doneLabel);   // mark the whole thread done
  }
}
