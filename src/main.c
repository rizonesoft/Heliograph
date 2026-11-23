#include <impossible/text.h>
#include "helio.h"

int im_main(void) {
    int count;
    char **args = im_args(&count);
    if (!args) return 3;
    int code = helio(count, args);
    im_args_free(args);
    return code;
}
