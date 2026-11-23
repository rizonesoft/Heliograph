/* Exits 0, or with the number of the first check that failed. */
#include <impossible/base.h>
#include "engine.h"

int im_main(void) {
    return engine_version() == 0 ? 0 : 1;
}
