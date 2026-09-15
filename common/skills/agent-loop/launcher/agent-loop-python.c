/* agent-loop-python: compiled, codesigned launcher giving macOS TCC a STABLE responsible
   process for the agent-loop LaunchAgents. It stays alive as the parent (launchd's job
   process) and runs /opt/homebrew/bin/python3 as a child, forwarding SIGTERM/SIGINT and
   the child's exit status. Homebrew's python3 is ad-hoc signed with a hash identifier
   that changes on rebuild, which is why TCC re-prompted on every launch. */
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

static pid_t child = 0;
static void forward(int sig) { if (child > 0) kill(child, sig); }

int main(int argc, char **argv) {
    child = fork();
    if (child < 0) { perror("fork"); return 127; }
    if (child == 0) {
        argv[0] = "/opt/homebrew/bin/python3";
        execv("/opt/homebrew/bin/python3", argv);
        perror("execv python3"); _exit(127);
    }
    signal(SIGTERM, forward); signal(SIGINT, forward); signal(SIGHUP, forward);
    int status = 0;
    while (waitpid(child, &status, 0) < 0) { /* EINTR from forwarded signals */ }
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    if (WIFSIGNALED(status)) { signal(WTERMSIG(status), SIG_DFL); raise(WTERMSIG(status)); }
    return 1;
}
