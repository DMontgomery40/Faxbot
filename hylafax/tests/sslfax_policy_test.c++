// Build-time check of Faxbot's SSL Fax address policy (faxd/sslfaxpolicy.h).
// The engine image build compiles and runs this after patching; any failure stops the build.
#include <arpa/inet.h>
#include <stdio.h>
#include "sslfaxpolicy.h"

static int failures = 0;

static uint32_t address(const char* text)
{
    struct in_addr in;
    if (inet_pton(AF_INET, text, &in) != 1) {
        printf("FAIL unparsable %s\n", text);
        failures++;
        return 0;
    }
    return ntohl(in.s_addr);
}

static void refused(const char* text, int expected)
{
    int actual = sslFaxRefusedAddress(address(text));
    if (actual != expected) {
        printf("FAIL %s refused=%d expected %d\n", text, actual, expected);
        failures++;
    }
}

static void own(const char* advertised, const char* mine, int expected)
{
    int actual = sslFaxOwnListener(advertised, mine);
    if (actual != expected) {
        printf("FAIL own(%s, %s)=%d expected %d\n", advertised ? advertised : "(null)", mine ? mine : "(null)",
               actual, expected);
        failures++;
    }
}

static void shown(const char* in, const char* expected)
{
    char out[160];
    sslFaxShown(in, out, sizeof (out));
    if (strcmp(out, expected) != 0) {
        printf("FAIL shown(%s)=%s expected %s\n", in, out, expected);
        failures++;
    }
}

int main()
{
    const char* never[] = {
        "0.0.0.0", "0.1.2.3", "10.0.0.1", "10.255.255.255", "100.64.0.1", "100.100.100.200", "100.127.255.255",
        "127.0.0.1", "127.255.255.254", "169.254.169.254", "169.254.0.1", "172.16.0.1", "172.31.255.255",
        "192.0.0.1", "192.0.0.192", "192.168.0.1", "192.168.255.255", "198.18.0.1", "198.19.255.255",
        "224.0.0.1", "239.255.255.250", "240.0.0.1", "255.255.255.255",
    };
    const char* allowed[] = {
        "1.1.1.1", "8.8.8.8", "100.63.255.255", "100.128.0.1", "172.15.255.255", "172.32.0.1", "192.0.1.1",
        "192.169.0.1", "198.17.255.255", "198.20.0.1", "198.51.100.10", "203.0.113.5", "223.255.255.255",
    };
    for (size_t i = 0; i < sizeof (never) / sizeof (never[0]); i++) refused(never[i], 1);
    for (size_t i = 0; i < sizeof (allowed) / sizeof (allowed[0]); i++) refused(allowed[i], 0);

    own("10.1.2.3:10443", "10.1.2.3:10443", 1);
    own("FAX.example.net:10443", "fax.example.net:10443", 1);
    own("10.1.2.3:10444", "10.1.2.3:10443", 0);
    own("10.1.2.4:10443", "10.1.2.3:10443", 0);
    own("10.1.2.3:10443", "", 0);
    own("10.1.2.3:10443", NULL, 0);
    own(NULL, "10.1.2.3:10443", 0);

    shown("ssl://A7SxrU7hSX@172.31.77.12:9999", "ssl://(passcode hidden)@172.31.77.12:9999");
    shown("ssl://172.31.77.12:9999", "ssl://172.31.77.12:9999");
    shown("ssl://a@b@host:1", "ssl://(passcode hidden)@host:1");
    shown("tel:+15555550100", "tel:+15555550100");
    shown("", "");

    if (failures) {
        printf("sslfax policy: %d failure(s)\n", failures);
        return 1;
    }
    printf("sslfax policy: all checks passed\n");
    return 0;
}
