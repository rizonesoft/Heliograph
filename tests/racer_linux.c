#include <signal.h>
int racer(int, char **);
int main(int count, char **args) {
    signal(SIGPIPE, SIG_IGN);
    return racer(count, args);
}
