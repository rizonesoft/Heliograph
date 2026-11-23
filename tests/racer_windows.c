#include <impossible/text.h>
int racer(int, char **);
int im_main(void) {
    int count;
    char **args = im_args(&count);
    return args ? racer(count, args) : 103;
}
