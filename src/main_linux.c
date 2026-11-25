#include <signal.h>
#include "helio.h"

int main(int count, char **args) {
    signal(SIGPIPE, SIG_IGN);
    return helio(count, args);
}
