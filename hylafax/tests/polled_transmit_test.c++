// Build-time check of the held-document rules for polled transmission (faxd/polldoc.h,
// hylafax/patches/0002-polled-transmit.patch). The engine image build compiles and runs this after patching;
// any failure stops the build.
#include <stdio.h>
#include <string.h>
#include "polldoc.h"

static int failures = 0;

static void check(int ok, const char* what)
{
    if (!ok) {
        printf("FAIL %s\n", what);
        failures++;
    }
}

static PollDoc held(const char* number, const char* selective, const char* password, long when)
{
    PollDoc doc;
    char text[512];
    snprintf(text, sizeof (text), "number=%s\nselective=%s\npassword=%s\njob=job-%ld\nheld=%ld\n",
             number, selective, password, when, when);
    check(pollDocParse(text, &doc), "a sidecar with a number parses");
    return doc;
}

int main()
{
    // Numbers: the caller's number as the trunk presents it against the one the document is held for.
    check(pollNumberMatch("+1 555 555 0199", "15555550199"), "formatting is ignored");
    check(pollNumberMatch("5555550199", "+15555550199"), "a number without its country code matches");
    check(!pollNumberMatch("15555550198", "15555550199"), "a different number does not match");
    check(!pollNumberMatch("0199", "15555550199"), "a short suffix is not enough");
    check(!pollNumberMatch("", "15555550199") && !pollNumberMatch("15555550199", ""), "an empty number matches nothing");

    // Sidecars: key=value lines, unknown keys ignored, the number required.
    PollDoc doc;
    check(pollDocParse("job=abc\nnumber=+1 (555) 555-0199\nselective= 12 34 \npassword=77\r\ntsi=+1 555 555 0100\n"
                       "tagline=From %s\nheld=1700000000\nfuture=ignored\n", &doc), "a full sidecar parses");
    check(strcmp(doc.number, "15555550199") == 0, "number keeps its digits only");
    check(strcmp(doc.selective, "1234") == 0 && strcmp(doc.password, "77") == 0, "SEP and PWD keep T.30 digits");
    check(strcmp(doc.job, "abc") == 0 && strcmp(doc.tsi, "+1 555 555 0100") == 0, "job and tsi are kept as given");
    check(strcmp(doc.tagline, "From %s") == 0 && doc.held == 1700000000L, "tagline and held time are kept");
    check(!pollDocParse("job=abc\nselective=1\n", &doc), "a sidecar without a number is not a held document");
    check(!pollDocParse("", &doc), "an empty sidecar is not a held document");

    // Choosing: the oldest document the DTC's SEP and PWD name; refusals say why.
    PollDoc docs[4];
    int reason = -1, chosen;
    docs[0] = held("15555550199", "", "", 300);
    docs[1] = held("15555550199", "", "", 100);
    docs[2] = held("15555550199", "42", "", 50);
    docs[3] = held("15555550199", "77", "2468", 10);

    chosen = pollChoose(docs, 4, "", "", &reason);
    check(chosen == 1 && reason == POLL_OK, "without SEP the oldest document held without a selective address goes");
    chosen = pollChoose(docs, 4, "42", "", &reason);
    check(chosen == 2 && reason == POLL_OK, "with SEP the document held for that address goes");
    chosen = pollChoose(docs, 4, "4 2", "", &reason);
    check(chosen == 2, "SEP digits are compared without spaces");
    chosen = pollChoose(docs, 4, "77", "2468", &reason);
    check(chosen == 3 && reason == POLL_OK, "the right password opens a protected document");
    chosen = pollChoose(docs, 4, "77", "0000", &reason);
    check(chosen == -1 && reason == POLL_WRONG_PASSWORD, "a wrong password is refused");
    chosen = pollChoose(docs, 4, "77", "", &reason);
    check(chosen == -1 && reason == POLL_WRONG_PASSWORD, "a missing password is refused");
    chosen = pollChoose(docs, 4, "99", "", &reason);
    check(chosen == -1 && reason == POLL_NO_SUCH_SELECTIVE, "an unknown SEP is refused");
    chosen = pollChoose(docs, 0, "", "", &reason);
    check(chosen == -1 && reason == POLL_NO_DOCUMENT, "nothing held is refused");
    chosen = pollChoose(docs + 2, 2, "", "", &reason);
    check(chosen == -1 && reason == POLL_NEEDS_SELECTIVE, "documents held with a SEP are never sent without one");
    chosen = pollChoose(docs, 4, "42", "1234", &reason);
    check(chosen == 2, "a password is ignored for a document held without one");

    check(strlen(pollRefusal(POLL_NO_DOCUMENT)) > 0 && strlen(pollRefusal(POLL_WRONG_PASSWORD)) > 0
          && strlen(pollRefusal(POLL_OK)) == 0, "every refusal has words and OK has none");

    if (failures) {
        printf("polled transmit: %d failure(s)\n", failures);
        return 1;
    }
    printf("polled transmit: all checks passed\n");
    return 0;
}
