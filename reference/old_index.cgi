#!/usr/bin/perl
use CGI;
use Data::Dumper;
use JSON;
use utf8;
use Encode;
use Time::HiRes qw();
use POSIX qw(strftime);

#use open ':std', ':encoding(UTF-8)';

binmode STDOUT, ':encoding(UTF-8)';
binmode STDERR, ':encoding(UTF-8)';

my $cgi    = new CGI;
my $json = JSON->new->allow_nonref;
$json = $json->pretty(1);

print "Content-Type: text/html; charset=UTF-8\n\n";
print qq#<head><meta charset="UTF-8">#;
print qq^
    <style>
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background-color: #eef2f5;
            margin: 0;
            padding: 20px;
        }
        .table-container {
            max-width: 100%;
            margin-bottom: 20px;
            overflow-x: auto;
        }
        table {
            width: 100%;
            border-collapse: separate; /* Use separate borders for spacing */
            border-spacing: 0 5px; /* Adds 5px space between rows */
            margin-top: 10px;
        }
        th {
            padding: 12px 15px;
            text-align: left;
            background-color: #4a90e2;
            color: white;
            font-weight: bold;
            position: sticky;
            top: 0;
            z-index: 1;
        }
        tr {
            background-color: white;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
            border-radius: 5px; /* Adds rounded corners to rows */
            transition: transform 0.2s, box-shadow 0.2s;
        }
        td {
            padding: 12px 15px;
        }
        tr:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 8px rgba(0, 0, 0, 0.15);
	    color: #ae0000;
        }
        .table-container::-webkit-scrollbar {
            height: 8px;
        }
        .table-container::-webkit-scrollbar-thumb {
            background-color: #ccc;
            border-radius: 5px;
        }
        .table-container::-webkit-scrollbar-track {
            background: #f0f0f0;
        }
    </style>
^;

print qq#</head><body>\n\n#;
#print qq#<body style="font-face:tahoma;font-size:16pt" bgcolor="\#eeeeee">#;



print qq#<h1>AI data collector</h1>\n#;
foreach ($cgi->param) {                                                                                                                                       
    $F{$_} = decode('UTF-8', $cgi->param($_));
}

sub read_file($)
{
    my $txt = undef;
    
    my $file = shift;
    open(my $fh, '<:encoding(UTF-8)', $file) or return undef;

  {
      local $/; # Enable 'slurp' mode
      $txt = <$fh>; # Read entire file into $content
  }

  close($fh);

    return $txt;
}

sub format_timestamp {
    my ($timestamp, $template) = @_;

    # Convert the timestamp from microseconds to seconds
    my $seconds      = int($timestamp / 1_000_000);
    my $microseconds = $timestamp % 1_000_000;

    # Convert microseconds to fractional seconds (up to 2 decimal places)
    my $fractional_seconds = sprintf("%.2f", $microseconds / 1_000_000);

    # Remove leading "0." from fractional seconds
    $fractional_seconds =~ s/^0\.//;

    # Convert to time components in UTC
    my @time_parts = gmtime($seconds);

    # Format the time using strftime
    my $formatted_time = strftime($template, @time_parts);

    # Replace '%f' with fractional seconds (2 decimal places)
    $formatted_time =~ s/%f/$fractional_seconds/e;

    return $formatted_time;
}


sub time_difference {
    my ($old, $new) = @_;

    # Convert timestamps from microseconds to seconds
    my $old_seconds      = int($old / 1_000_000);
    my $old_microseconds = $old % 1_000_000;

    my $new_seconds      = int($new / 1_000_000);
    my $new_microseconds = $new % 1_000_000;

    # Calculate the difference in seconds and microseconds
    my $diff_seconds      = $new_seconds - $old_seconds;
    my $diff_microseconds = $new_microseconds - $old_microseconds;

    # Adjust if microseconds are negative
    if ($diff_microseconds < 0) {
        $diff_microseconds += 1_000_000;
        $diff_seconds -= 1;
    }

    # Break down the difference
    my $minutes = int($diff_seconds / 60);
    my $seconds = $diff_seconds % 60 + $diff_microseconds / 1_000_000;

    # Build the result string
    my $result = sprintf("%dm %.1fs", $minutes, $seconds);

    return $result;
}

if ($F{id}) {
  my $id = $F{id};

  $id =~ s/[^\w\d\-]//;


  my $jtxt = read_file("logs/$id") or die;

  my $obj = $json->decode($jtxt);

  #print "<pre>" . Dumper($obj->{global_data});

  print qq#<a href="$ENV{SCRIPT_NAME}"><--back</a>#;

  print "<h2>Conversation Log</h2>";

  if ($obj->{SWMLVars}->{record_call_url}) {
      printf qq#<a href="$obj->{SWMLVars}->{record_call_url}">Recording</a><br><br>#;
  }

  if ($obj->{total_minutes}) {
      print qq#<table cellspacing=0 cellpadding=5 border=0 width=400 style="font-face:tahoma;font-size:16pt">#;
      print qq#<tr><td>Minutes:</td><td colspan=2>$obj->{total_minutes}</td></tr>#;
      print qq#<tr><td>Tokens In:</td><td>$obj->{total_wire_input_tokens}</td><td> $obj->{total_wire_input_tokens_per_minute}</td></tr>#;
      print qq#<tr><td>Tokens Out:</td><td>$obj->{total_wire_output_tokens} </td><td> $obj->{total_wire_output_tokens_per_minute}</td></tr>#;      
      print qq#<tr><td>TTS Chars</td><td>$obj->{total_tts_chars} </td><td> $obj->{total_tts_chars_per_min}</td></tr>#;
      print qq#<tr><td>ASR</td><td>$obj->{total_asr_minutes}</td><td>$obj->{total_asr_cost_factor}</td></tr>#;
      print qq#</table><br><br>#;
  }
  
  
  my $convo = $obj->{call_log};
  #print "<pre>";
  #print Dumper $obj;
  #print "</pre>";
  print qq#<table cellspacing=0 cellpadding=5 border=0 width=1200 style="font-face:tahoma;font-size:16pt">\n#;
  foreach (@{$convo}) {
    my $style;
    if ($_->{role} eq "system") {
      $style = qq#style="background-color:\#aa1111;color:white"#;
    } elsif ($_->{role} eq "system-log") {
      $style = qq#style="background-color:700000;color:white"#;
    } elsif ($_->{role} eq "user") {
      $style = qq#style="background-color:\#2a4d69;color:white"#;
    } elsif ($_->{role} eq "function" || $_->{role} eq "tool") {
	$_->{role} = "function";
      $style = qq#style="background-color: purple;color:white"#;	
    } else {
      $style = qq#style="background-color:\#4b86b4;color:white"#;
    }
    my $content = $_->{content};
    my $tool_calls = $_->{tool_calls};
    
    if (!$content && $tool_calls) {
	#print Dumper $tool_calls;
	next;
	#$content = "Executed Function: " . $tool_calls->[0]->{function}->{name};
    }
    
    $content =~ s/\</&lt;/mg;
    $content =~ s/\>/&gt;/mg;
    $content =~ s/\n/<br>/mg;
    #$content =~ s/ /\&nbsp;/mg;
    my $latency = "";
    
    if ($_->{"latency"}) {
	#$latency = "<br><br><i>(Latency: $_->{latency}ms / $_->{utterance_latency}ms / $_->{audio_latency}ms)</i>";
	$xlatency =  $_->{latency};
	$xutterance_latency = $_->{utterance_latency} - ${xlatency};
	$xaudio_latency = $_->{audio_latency} - $xlatency - $xutterance_latency;
	$latency = "<br><br><i>Latency: $_->{audio_latency}ms (${xlatency}ms / ${xutterance_latency}ms / ${xaudio_latency}ms)</i>";
    }



    
    my $confidence = "";

    if ($_->{confidence}) {
	$confidence .= sprintf "<br><br><i>Confidence: %0.2f%%</i>", $_->{confidence} * 100;
    }

    if ($_->{speaker}) {
	$confidence .= sprintf " | <i>Speaker: %s</i>", $_->{speaker};
    }
    

    
    my $timestamp = "";
    if ($_->{timestamp}) {
	if ($last_timestamp) {
	    $el = "[Elapsed: " . time_difference($last_timestamp, $_->{timestamp}) . "]";
	} else {
	    $el = "";
	}
	$timestamp = format_timestamp($_->{timestamp}, "(%Y-%m-%d %H:%M:%S.%f UTC) $el");
    }

    $last_timestamp = $_->{timestamp};
    
    
    print qq#<tr $style><td valign=top width=150><b>$_->{role}: ${timestamp}</b><br><br>$content${latency}${confidence}<br><br></td></tr>\n#;
  }
  print "</table>";

  print "<h2>Specific Collected Data</h2>";
  print qq#<table cellspacing=1 cellpadding=5 width=1200 style="font-face:tahoma;font-size:16pt;max-width: 1200px">\n#;
  print qq#<tr><td>$obj->{post_prompt_data}->{raw}</td></tr>#;

  my $global_data = $json->encode($obj->{global_data});
  
  print qq#<tr><td><pre>$global_data</pre></td></tr>#;  
  print "</table>";
  print "<br><br><br>";
  exit;
}

#opendir(my $dh, logs) || die "Can't opendir logs: $!";
#my @files = grep { !/^\./ && -f "logs/$_" } readdir($dh);
#closedir $dh;

my @files = split("\n", `(cd logs && ls -1t *-*)`);

use POSIX qw(strftime);

print qq#
    <div class="table-container">
#;

print qq#<table>#;
#print qq#<table border=0 cellpadding=5 style="font-face:tahoma;font-size:16pt" width=1600>#;
print qq#<thead><tr><th><b>Date</b></th><th><b>Name</b></th><th><b>Number</b></th><th><b>File</b></th></tr></thead><tbody>#;
foreach(@files) {

    my $jtxt = read_file("logs/$_") or die;
    


    #$jtxt = decode('UTF-8', $jtxt);
    my $obj = $json->decode($jtxt);
    
    my ($dev,$ino,$mode,$nlink,$uid,$gid,$rdev,$size,
	$atime,$mtime,$ctime,$blksize,$blocks)
	= stat("logs/$_");
    my $timestamp       = localtime($ctime);


    my $timestamp = strftime "%Y-%m-%d %H:%M", localtime($ctime);

    
    my ($name) = $obj->{caller_id_name};
    my ($num) = $obj->{caller_id_num};
    
    print "<tr><td>$timestamp</td><td>$name</td><td>$num</td><td><a href=${SCRIPT_NAME}?id=$_>$_</a></td></tr>\n";
}

print "</tbody></table></div>";
